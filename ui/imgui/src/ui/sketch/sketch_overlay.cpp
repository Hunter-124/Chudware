// Sketch mode (H5 port from host/src/ui/sketch_mode.cpp). See sketch_mode.h.
#include "sketch_mode.h"
#include "sketch_internal.h"
#include "sketch_seam.h"
#include "sketch_palette.h"
#include "airfoil.h"
#include "platform/file_dialog.h"

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <functional>
#include <set>

using ui::sketch::SketchHost;
using ui::sketch::worldToScreen;
using ui::sketch::screenRay;
using ui::sketch::arcSweepContains;
using ui::sketch::col;
using ui::sketch::kSnapPx;
using ui::sketch::kHandlePx;
using ui::sketch::parseEntities;
using ui::sketch::skBaseId;
using ui::sketch::sketchParamsInTree;
using ui::sketch::resyncMirror;
using ui::sketch::catmullRomUV;
using ui::sketch::kGridStep;
using ui::sketch::kTangentDeg;
using ui::sketch::kInferHVDeg;
using ui::sketch::kHVDeg;
using ui::sketch::snapHasPrefix;
using ui::sketch::coincRef;
using ui::sketch::circumcircle;
using ui::sketch::drawDashedLine;
using ui::sketch::drawDashedPolyline;
using ui::sketch::g_skIps;
using ui::sketch::fmtLen;
using ui::sketch::fmtAng;
using ui::sketch::skLenUnit;
using ui::sketch::arrowHead;
using ui::sketch::toWorkerPick;
#define skPal ui::sketch::sketchPalette()

namespace ui {
void SketchMode::refreshGhosts(SketchHost& st) {
    const std::string docId = st.docMirror().docId();
    const int rev = st.docMirror().revision();
    // C48: the in-flight create-mode PM preview (Extrude/Revolve...) consumes its profile
    // sketch in the doc, but the curves must STAY visible until commit (SolidWorks shows the
    // profile through the preview). A change of the exempt id also forces a refresh.
    std::string exemptFid;
    if (st.propertyManager) exemptFid = st.propertyManager->previewFeatureId();
    if (docId == ghostsDocId_ && rev == ghostsRevision_ && exemptFid == ghostsExemptFid_) return;
    ghostsDocId_ = docId;
    ghostsRevision_ = rev;
    ghostsExemptFid_ = exemptFid;
    ++ghostsGen_;
    // C48: keep already-loaded snapshots across the refresh — during PM preview churn the
    // revision bumps faster than the async overview fetch completes, and clearing here made
    // every unconsumed sketch flicker out (or vanish for the whole preview session). Stale
    // coords draw until the fresh fetch lands.
    std::vector<GhostSketch> old;
    old.swap(ghosts_);
    const nlohmann::json& tree = st.docMirror().tree();
    if (!tree.is_object() || !tree.contains("features") || !tree["features"].is_array()) return;
    const auto& feats = tree["features"];
    // Per-sketch consumer scan: a sketch is consumed when a DIFFERENT feature references it
    // ("sketch":"<fid>" inside a region selector) AND that consumer is actually built —
    // sketches referenced by an errored or rolled-back feature still ghost (the user needs to
    // see them to fix things), as does the PM's exempt preview feature.
    const nlohmann::json& statusArr = st.docMirror().featureStatus();
    auto consumerCounts = [&](const std::string& fid) -> bool {
        const std::string needle = "\"sketch\":\"" + fid + "\"";
        for (const auto& f : feats) {
            if (!f.is_object() || f.value("type", std::string()) == "sketch") continue;
            const std::string cid = f.value("id", std::string());
            if (cid.empty() || cid == exemptFid) continue;
            if (!f.contains("refs") || f["refs"].dump().find(needle) == std::string::npos)
                continue;
            // Consumer found — does it actually build? (missing status record = assume ok)
            bool ok = true, rolledBack = false;
            if (statusArr.is_array()) {
                for (const auto& s : statusArr) {
                    if (!s.is_object() || s.value("id", std::string()) != cid) continue;
                    ok = s.value("ok", true);
                    rolledBack = s.value("rolled_back", false);
                    break;
                }
            }
            if (ok && !rolledBack) return true;
        }
        return false;
    };
    for (const auto& f : feats) {
        if (!f.is_object() || f.value("type", std::string()) != "sketch") continue;
        if (f.value("suppressed", false)) continue;
        const std::string fid = f.value("id", std::string());
        if (fid.empty()) continue;
        if (!f.contains("params") || !f["params"].is_object()) continue;
        const auto& ents = f["params"].value("entities", nlohmann::json::array());
        if (!ents.is_array() || ents.empty()) continue;
        if (consumerCounts(fid)) continue;
        ghosts_.push_back(GhostSketch{});
        ghosts_.back().fid = fid;
        // Carry over the previous snapshot (stale-but-visible) while the refresh is in flight.
        for (auto& o : old) {
            if (o.fid != fid || !o.loaded) continue;
            std::memcpy(ghosts_.back().frame, o.frame, sizeof(o.frame));
            ghosts_.back().ents = std::move(o.ents);
            ghosts_.back().loaded = true;
            break;
        }
        // Fetch the solved coords + plane frame asynchronously; the callback runs on the GL
        // thread (AsyncRpc::drain) and is dropped if the doc moved on (gen guard) — harmless
        // now, since the carried-over snapshot keeps drawing until a fresh fetch lands.
        const int gen = ghostsGen_;
        st.rpc().call("doc_sketch_overview", {{"doc_id", docId}, {"sketch", fid}},
                    [this, gen, fid](const core::RpcResult& r) {
                        if (gen != ghostsGen_) return;
                        if (!r.ok || !r.result.is_object() || r.result.contains("code")) return;
                        for (auto& g : ghosts_) {
                            if (g.fid != fid) continue;
                            if (r.result.contains("plane_frame") &&
                                r.result["plane_frame"].is_array() &&
                                r.result["plane_frame"].size() == 16)
                                for (int i = 0; i < 16; ++i)
                                    g.frame[i] = r.result["plane_frame"][i].get<float>();
                            parseEntities(r.result, g.ents);
                            g.loaded = true;
                        }
                    });
    }
}

void SketchMode::drawGhosts(SketchHost& st, const ImVec2& imgMin, const ImVec2& imgMax,
                            int vw, int vh) {
    if (!st.renderer || !st.workerStarted() || !st.docMirror().hasDoc()) {
        if (!ghosts_.empty()) { ghosts_.clear(); ghostsDocId_.clear(); ghostsRevision_ = -999; }
        return;
    }
    refreshGhosts(st);
    if (ghosts_.empty()) return;
    const auto& cam = st.renderer->camera();
    ImDrawList* dl = ImGui::GetWindowDrawList();
    dl->PushClipRect(imgMin, imgMax, true);
    const auto& pal = skPal;
    for (const auto& g : ghosts_) {
        if (!g.loaded) continue;
        if (active_ && g.fid == fid_) continue;   // the live overlay owns the active sketch
        if (st.hiddenFeatures.count(g.fid)) continue;   // FeatureManager eye toggled this sketch off
        // Project sketch (u,v) through this ghost's own plane frame (no shared basis).
        auto uv2s = [&](float u, float v, ImVec2& o) -> bool {
            float wx = g.frame[0] * u + g.frame[1] * v + g.frame[3];
            float wy = g.frame[4] * u + g.frame[5] * v + g.frame[7];
            float wz = g.frame[8] * u + g.frame[9] * v + g.frame[11];
            return worldToScreen(cam, imgMin, vw, vh, wx, wy, wz, o);
        };
        auto poly = [&](auto pointAt, int seg) {     // sample pointAt(t) for t in [0,1]
            ImVec2 prev; bool havePrev = false;
            for (int i = 0; i <= seg; ++i) {
                float u, v; pointAt(i / (float)seg, u, v);
                ImVec2 p;
                if (!uv2s(u, v, p)) { havePrev = false; continue; }
                if (havePrev)
                    dl->AddLine(prev, p, col(pal.sketchInk, g.fid == fid_ ? 0.9f : 0.55f), 1.5f);
                prev = p; havePrev = true;
            }
        };
        const float D2R = 3.14159265f / 180.0f;
        for (const auto& e : g.ents) {
            if (e.construction) continue;   // ghost view: skip construction geometry
            if (e.type == "line") {
                poly([&](float t, float& u, float& v) {
                    u = e.p1[0] + (e.p2[0] - e.p1[0]) * t;
                    v = e.p1[1] + (e.p2[1] - e.p1[1]) * t;
                }, 1);
            } else if (e.type == "slot") {
                // Closed capsule: two side lines (tangent, offset by half-width) + two end-cap
                // half-circles — the same profile the engine extrudes.
                float dx = e.p2[0] - e.p1[0], dy = e.p2[1] - e.p1[1];
                float L = std::hypot(dx, dy);
                float hw = e.width * 0.5f;
                float nx = L > 1e-6f ? -dy / L : 0.0f, ny = L > 1e-6f ? dx / L : 0.0f;
                for (float s : {1.0f, -1.0f})
                    poly([&](float t, float& u, float& v) {
                        u = (e.p1[0] + nx * hw * s) + (e.p2[0] - e.p1[0]) * t;
                        v = (e.p1[1] + ny * hw * s) + (e.p2[1] - e.p1[1]) * t;
                    }, 1);
                if (hw > 0.0f) {
                    float ax1 = std::atan2(-dy, -dx), ax2 = std::atan2(dy, dx);
                    for (auto pr : {std::make_pair(e.p1, ax1), std::make_pair(e.p2, ax2)}) {
                        const float* c = pr.first; float axis = pr.second;
                        poly([&](float t, float& u, float& v) {
                            float a = axis - 1.5707963f + 3.14159265f * t;
                            u = c[0] + hw * std::cos(a);
                            v = c[1] + hw * std::sin(a);
                        }, 24);
                    }
                }
            } else if (e.type == "circle") {
                poly([&](float t, float& u, float& v) {
                    float a = t * 6.2831853f;
                    u = e.center[0] + e.radius * std::cos(a);
                    v = e.center[1] + e.radius * std::sin(a);
                }, 48);
            } else if (e.type == "arc" || e.type == "arc_slot") {
                float a0 = e.start, a1 = e.end;
                if (e.type == "arc" && !e.ccw) { float t = a0; a0 = a1; a1 = t; }
                if (a1 < a0) a1 += 360.0f;
                auto arcAt = [&](float rad, float t, float& u, float& v) {
                    float a = (a0 + (a1 - a0) * t) * D2R;
                    u = e.center[0] + rad * std::cos(a);
                    v = e.center[1] + rad * std::sin(a);
                };
                if (e.type == "arc") {
                    poly([&](float t, float& u, float& v) { arcAt(e.radius, t, u, v); }, 40);
                } else {
                    float hw = e.width * 0.5f;
                    poly([&](float t, float& u, float& v) { arcAt(e.radius + hw, t, u, v); }, 40);
                    poly([&](float t, float& u, float& v) { arcAt(e.radius - hw, t, u, v); }, 40);
                    for (float aDeg : {e.start, e.end}) {
                        float sa = aDeg * D2R;
                        float cx = e.center[0] + e.radius * std::cos(sa);
                        float cy = e.center[1] + e.radius * std::sin(sa);
                        poly([&](float t, float& u, float& v) {
                            float a = t * 6.2831853f;
                            u = cx + hw * std::cos(a);
                            v = cy + hw * std::sin(a);
                        }, 24);
                    }
                }
            } else if (e.type == "ellipse") {
                // C42: rotated parametric sample (rot in degrees).
                float ca = std::cos(e.rot * D2R), sa = std::sin(e.rot * D2R);
                poly([&](float t, float& u, float& v) {
                    float a = t * 6.2831853f;
                    float lx = e.rx * std::cos(a), ly = e.ry * std::sin(a);
                    u = e.center[0] + lx * ca - ly * sa;
                    v = e.center[1] + lx * sa + ly * ca;
                }, 48);
            } else if ((e.type == "spline" || e.type == "style_spline") && e.pts.size() >= 2) {
                // C42/M-1: a "spline" ghosts its SMOOTH curve (Catmull-Rom through-points) so it
                // doesn't read as sharp-cornered; a "style_spline" ghosts its control polygon.
                const std::vector<ImVec2>& gp =
                    (e.type == "spline") ? catmullRomUV(e.pts) : e.pts;
                for (size_t i = 1; i < gp.size(); ++i) {
                    ImVec2 a, b;
                    if (uv2s(gp[i - 1].x, gp[i - 1].y, a) && uv2s(gp[i].x, gp[i].y, b))
                        dl->AddLine(a, b, col(pal.sketchInk, 0.55f), 1.5f);
                }
            } else if (e.type == "point") {
                // C42: small ghost cross.
                ImVec2 p;
                if (uv2s(e.p1[0], e.p1[1], p)) {
                    const ImU32 gc = col(pal.sketchInk, 0.55f);
                    dl->AddLine(ImVec2(p.x - 4, p.y), ImVec2(p.x + 4, p.y), gc, 1.5f);
                    dl->AddLine(ImVec2(p.x, p.y - 4), ImVec2(p.x, p.y + 4), gc, 1.5f);
                }
            }
            // conic-family ghosts: not yet sampled (rare as standalone profiles).
        }
    }

    // --- persistent selected-path highlight (sweep / loft) ----------------------
    // Keep the chosen Path + Guides geometry boldly highlighted while the form is open,
    // even when the live preview is failing — so the user always sees what's selected.
    if (auto* pm = st.propertyManager; pm && pm->active()) {
        std::set<std::string> hi;
        for (const auto& id : pm->pathHighlightFeatureIds()) hi.insert(id);
        if (!hi.empty()) {
            const ImU32 hc = col(pal.accent, 1.0f);
            const float D2R = 3.14159265f / 180.0f;
            // (a) sketch paths/guides: redraw the matching ghost sketches bold.
            for (const auto& g : ghosts_) {
                if (!g.loaded || !hi.count(g.fid)) continue;
                auto uv2s = [&](float u, float v, ImVec2& o) -> bool {
                    float wx = g.frame[0] * u + g.frame[1] * v + g.frame[3];
                    float wy = g.frame[4] * u + g.frame[5] * v + g.frame[7];
                    float wz = g.frame[8] * u + g.frame[9] * v + g.frame[11];
                    return worldToScreen(cam, imgMin, vw, vh, wx, wy, wz, o);
                };
                auto poly = [&](auto pointAt, int seg) {
                    ImVec2 prev; bool have = false;
                    for (int i = 0; i <= seg; ++i) {
                        float u, v; pointAt(i / (float)seg, u, v);
                        ImVec2 p;
                        if (!uv2s(u, v, p)) { have = false; continue; }
                        if (have) dl->AddLine(prev, p, hc, 2.75f);
                        prev = p; have = true;
                    }
                };
                for (const auto& e : g.ents) {
                    if (e.construction) continue;
                    if (e.type == "line" || e.type == "slot" || e.type == "arc_slot") {
                        poly([&](float t, float& u, float& v) {
                            u = e.p1[0] + (e.p2[0] - e.p1[0]) * t;
                            v = e.p1[1] + (e.p2[1] - e.p1[1]) * t; }, 1);
                    } else if (e.type == "circle") {
                        poly([&](float t, float& u, float& v) {
                            float a = t * 6.2831853f;
                            u = e.center[0] + e.radius * std::cos(a);
                            v = e.center[1] + e.radius * std::sin(a); }, 48);
                    } else if (e.type == "arc") {
                        float a0 = e.start, a1 = e.end;
                        if (!e.ccw) { float t = a0; a0 = a1; a1 = t; }
                        if (a1 < a0) a1 += 360.0f;
                        poly([&](float t, float& u, float& v) {
                            float a = (a0 + (a1 - a0) * t) * D2R;
                            u = e.center[0] + e.radius * std::cos(a);
                            v = e.center[1] + e.radius * std::sin(a); }, 40);
                    } else if (e.type == "ellipse") {
                        float ca = std::cos(e.rot * D2R), sa = std::sin(e.rot * D2R);
                        poly([&](float t, float& u, float& v) {
                            float a = t * 6.2831853f;
                            float lx = e.rx * std::cos(a), ly = e.ry * std::sin(a);
                            u = e.center[0] + lx * ca - ly * sa;
                            v = e.center[1] + lx * sa + ly * ca; }, 48);
                    } else if ((e.type == "spline" || e.type == "style_spline") && e.pts.size() >= 2) {
                        for (size_t i = 1; i < e.pts.size(); ++i) {
                            ImVec2 a, b;
                            if (uv2s(e.pts[i - 1].x, e.pts[i - 1].y, a) && uv2s(e.pts[i].x, e.pts[i].y, b))
                                dl->AddLine(a, b, hc, 2.75f);
                        }
                    }
                }
            }
            // (b) curve paths (helix/spiral): draw the matching curve datum bold.
            const nlohmann::json& datums = st.docMirror().refDatums();
            if (datums.is_array()) {
                for (const auto& d : datums) {
                    if (!d.is_object() || d.value("kind", std::string()) != "curve") continue;
                    if (!hi.count(d.value("id", std::string()))) continue;
                    const auto& pts = d.contains("points") ? d["points"] : nlohmann::json();
                    if (!pts.is_array()) continue;
                    ImVec2 prev; bool have = false;
                    for (const auto& p : pts) {
                        ImVec2 s; bool ok = p.is_array() && p.size() >= 3 &&
                            worldToScreen(cam, imgMin, vw, vh, (float)p[0].get<double>(),
                                          (float)p[1].get<double>(), (float)p[2].get<double>(), s);
                        if (!ok) { have = false; continue; }
                        if (have) dl->AddLine(prev, s, hc, 2.75f);
                        prev = s; have = true;
                    }
                }
            }
        }
    }
    dl->PopClipRect();
}

// ---------------------------------------------------------------------------
// C51: feature-PM region picker — hover-highlight + click-to-toggle profile regions
// ---------------------------------------------------------------------------

void SketchMode::refreshRegionPicker(SketchHost& st) {
    const std::string docId = st.docMirror().docId();
    const int rev = st.docMirror().revision();
    auto* pm = st.propertyManager;
    std::vector<std::string> cands = pm ? pm->regionCandidateSketches(st)
                                        : std::vector<std::string>();
    std::string key;
    for (const auto& c : cands) { key += c; key += ','; }
    if (docId == pickDocId_ && rev == pickRevision_ && key == pickCandidatesKey_) return;
    pickDocId_ = docId; pickRevision_ = rev; pickCandidatesKey_ = key;
    ++pickGen_;
    // Carry stale snapshots across the refresh (avoid flicker during PM preview revision churn).
    std::vector<RegionSketch> old; old.swap(pickRegions_);
    for (const auto& fid : cands) {
        pickRegions_.push_back(RegionSketch{});
        pickRegions_.back().fid = fid;
        for (auto& o : old) {
            if (o.fid != fid || !o.loaded) continue;
            std::memcpy(pickRegions_.back().frame, o.frame, sizeof(o.frame));
            pickRegions_.back().regions = std::move(o.regions);
            pickRegions_.back().loaded = true;
            break;
        }
        const int gen = pickGen_;
        st.rpc().call("doc_sketch_regions", {{"doc_id", docId}, {"sketch", fid}},
            [this, gen, fid](const core::RpcResult& r) {
                if (gen != pickGen_) return;
                if (!r.ok || !r.result.is_object() || r.result.contains("code")) return;
                for (auto& rs : pickRegions_) {
                    if (rs.fid != fid) continue;
                    if (r.result.contains("plane_frame") && r.result["plane_frame"].is_array() &&
                        r.result["plane_frame"].size() == 16)
                        for (int i = 0; i < 16; ++i)
                            rs.frame[i] = r.result["plane_frame"][i].get<float>();
                    rs.regions.clear();
                    if (r.result.contains("regions") && r.result["regions"].is_array()) {
                        for (const auto& jr : r.result["regions"]) {
                            PickRegion pr;
                            pr.index = jr.value("index", 0);
                            pr.isMaterial = jr.value("is_material", true);
                            if (jr.contains("centroid") && jr["centroid"].is_array() &&
                                jr["centroid"].size() == 2) {
                                pr.centroid[0] = jr["centroid"][0].get<float>();
                                pr.centroid[1] = jr["centroid"][1].get<float>();
                            }
                            auto flat2 = [](const nlohmann::json& arr, std::vector<float>& out) {
                                if (!arr.is_array()) return;
                                for (const auto& p : arr)
                                    if (p.is_array() && p.size() == 2) {
                                        out.push_back(p[0].get<float>());
                                        out.push_back(p[1].get<float>());
                                    }
                            };
                            if (jr.contains("outer")) flat2(jr["outer"], pr.outer);
                            if (jr.contains("holes") && jr["holes"].is_array())
                                for (const auto& h : jr["holes"]) {
                                    pr.holes.emplace_back();
                                    flat2(h, pr.holes.back());
                                }
                            if (jr.contains("tris") && jr["tris"].is_array())
                                for (const auto& t : jr["tris"])
                                    if (t.is_array() && t.size() == 3)
                                        for (const auto& p : t)
                                            if (p.is_array() && p.size() == 2) {
                                                pr.tris.push_back(p[0].get<float>());
                                                pr.tris.push_back(p[1].get<float>());
                                            }
                            rs.regions.push_back(std::move(pr));
                        }
                    }
                    rs.loaded = true;
                }
            });
    }
}

// Screen-space point-in-polygon over a flat u,v list already projected to screen pts.
static bool pmPtInScreenPoly(const std::vector<ImVec2>& poly, const ImVec2& p) {
    bool in = false;
    size_t n = poly.size();
    if (n < 3) return false;
    for (size_t i = 0, j = n - 1; i < n; j = i++) {
        const ImVec2& a = poly[i];
        const ImVec2& b = poly[j];
        if (((a.y > p.y) != (b.y > p.y)) &&
            (p.x < (b.x - a.x) * (p.y - a.y) / (b.y - a.y + 1e-9f) + a.x))
            in = !in;
    }
    return in;
}

bool SketchMode::drawRegionPicker(SketchHost& st, ImGuiIO& io, const ImVec2& imgMin,
                                  const ImVec2& imgMax, int vw, int vh, bool imgHovered) {
    if (active_) return false;                 // editing a sketch: the overlay owns the canvas
    auto* pm = st.propertyManager;
    if (!st.renderer || !st.workerStarted() || !st.docMirror().hasDoc() ||
        !pm || !pm->active() || !pm->wantsRegionPicker()) {
        if (!pickRegions_.empty()) { pickRegions_.clear(); pickCandidatesKey_.clear(); }
        return false;
    }
    refreshRegionPicker(st);
    if (pickRegions_.empty()) return false;

    const auto& cam = st.renderer->camera();
    ImDrawList* dl = ImGui::GetWindowDrawList();
    dl->PushClipRect(imgMin, imgMax, true);
    const auto& pal = skPal;
    const bool ordered = pm->sectionsAreOrdered();

    // Parse the current profile/sections selection.
    std::vector<nlohmann::json> picks = pm->profilePicks();
    auto isWhole = [&](const std::string& fid) -> bool {
        for (const auto& s : picks)
            if (s.value("kind", std::string()) == "region" &&
                s.value("sketch", std::string()) == fid && !s.contains("index"))
                return true;
        return false;
    };
    auto explicitHas = [&](const std::string& fid, int idx) -> bool {
        for (const auto& s : picks)
            if (s.value("kind", std::string()) == "region" &&
                s.value("sketch", std::string()) == fid && s.contains("index") &&
                s["index"].is_number() && (int)s["index"].get<double>() == idx)
                return true;
        return false;
    };
    auto isSelected = [&](const std::string& fid, const PickRegion& pr) -> bool {
        if (explicitHas(fid, pr.index)) return true;
        return isWhole(fid) && pr.isMaterial;   // whole sketch = all material regions
    };

    // Project a region's outer + hole polylines for the given sketch frame.
    auto projectPoly = [&](const float frame[16], const std::vector<float>& uv,
                           std::vector<ImVec2>& out) {
        out.clear();
        for (size_t i = 0; i + 1 < uv.size(); i += 2) {
            float u = uv[i], v = uv[i + 1];
            float wx = frame[0] * u + frame[1] * v + frame[3];
            float wy = frame[4] * u + frame[5] * v + frame[7];
            float wz = frame[8] * u + frame[9] * v + frame[11];
            ImVec2 p;
            if (worldToScreen(cam, imgMin, vw, vh, wx, wy, wz, p)) out.push_back(p);
        }
    };

    // ---- hover detection (screen space, even-odd hole aware) ----
    int hovS = -1, hovR = -1;
    float hovArea = 1e30f;
    const ImVec2 mouse = io.MousePos;
    const bool canHover = imgHovered && mouse.x >= imgMin.x && mouse.x <= imgMax.x &&
                          mouse.y >= imgMin.y && mouse.y <= imgMax.y;
    std::vector<ImVec2> outerS, holeS;
    if (canHover) {
        for (size_t si = 0; si < pickRegions_.size(); ++si) {
            const auto& rs = pickRegions_[si];
            if (!rs.loaded) continue;
            for (size_t ri = 0; ri < rs.regions.size(); ++ri) {
                const auto& pr = rs.regions[ri];
                projectPoly(rs.frame, pr.outer, outerS);
                if (!pmPtInScreenPoly(outerS, mouse)) continue;
                bool inHole = false;
                for (const auto& h : pr.holes) {
                    projectPoly(rs.frame, h, holeS);
                    if (pmPtInScreenPoly(holeS, mouse)) { inHole = true; break; }
                }
                if (inHole) continue;
                // Screen-area tie-break: prefer the smallest region under the cursor.
                float a = 0.0f;
                for (size_t k = 0, j = outerS.size() - 1; k < outerS.size(); j = k++)
                    a += (outerS[j].x + outerS[k].x) * (outerS[j].y - outerS[k].y);
                a = std::fabs(a) * 0.5f;
                if (a < hovArea) { hovArea = a; hovS = (int)si; hovR = (int)ri; }
            }
        }
    }

    // ---- draw: fills (selected/hover) + outlines ----
    std::vector<ImVec2> tri(3);
    for (size_t si = 0; si < pickRegions_.size(); ++si) {
        const auto& rs = pickRegions_[si];
        if (!rs.loaded) continue;
        for (size_t ri = 0; ri < rs.regions.size(); ++ri) {
            const auto& pr = rs.regions[ri];
            const bool sel = isSelected(rs.fid, pr);
            const bool hov = ((int)si == hovS && (int)ri == hovR);
            // Hole-aware fill from engine triangles when selected or hovered.
            if ((sel || hov) && pr.tris.size() >= 6) {
                ImU32 fillc = sel ? col(pal.accent, hov ? 0.42f : 0.28f)
                                  : col(pal.hoverHighlight, 0.5f);
                for (size_t t = 0; t + 5 < pr.tris.size(); t += 6) {
                    bool ok = true;
                    for (int k = 0; k < 3; ++k) {
                        float u = pr.tris[t + k * 2], v = pr.tris[t + k * 2 + 1];
                        float wx = rs.frame[0] * u + rs.frame[1] * v + rs.frame[3];
                        float wy = rs.frame[4] * u + rs.frame[5] * v + rs.frame[7];
                        float wz = rs.frame[8] * u + rs.frame[9] * v + rs.frame[11];
                        if (!worldToScreen(cam, imgMin, vw, vh, wx, wy, wz, tri[k])) { ok = false; break; }
                    }
                    if (ok) dl->AddTriangleFilled(tri[0], tri[1], tri[2], fillc);
                }
            }
            // Outline: outer + holes. Selected = accent bold, else faint dim line.
            ImU32 lc = sel ? col(pal.accent, 0.95f)
                           : (hov ? col(pal.accent, 0.6f) : col(pal.sketchDimLine, 0.5f));
            float th = sel ? 2.2f : (hov ? 1.8f : 1.2f);
            projectPoly(rs.frame, pr.outer, outerS);
            if (outerS.size() >= 2) dl->AddPolyline(outerS.data(), (int)outerS.size(), lc, ImDrawFlags_Closed, th);
            for (const auto& h : pr.holes) {
                projectPoly(rs.frame, h, holeS);
                if (holeS.size() >= 2) dl->AddPolyline(holeS.data(), (int)holeS.size(), lc, ImDrawFlags_Closed, th);
            }
        }
    }
    dl->PopClipRect();

    // ---- click: toggle the hovered region into/out of the profile ----
    if (hovS >= 0 && canHover && ImGui::IsMouseClicked(ImGuiMouseButton_Left)) {
        st.requestCursor(ui::sketch::CursorKind::Hand);
        const std::string fid = pickRegions_[hovS].fid;
        const int idx = pickRegions_[hovS].regions[hovR].index;

        std::vector<nlohmann::json> out;
        if (ordered) {
            // Loft sections: clicks APPEND in order; re-clicking removes.
            bool removed = false;
            for (const auto& s : picks) {
                if (s.value("kind", std::string()) == "region" &&
                    s.value("sketch", std::string()) == fid && s.contains("index") &&
                    s["index"].is_number() && (int)s["index"].get<double>() == idx) {
                    removed = true; continue;  // drop it
                }
                out.push_back(s);
            }
            if (!removed)
                out.push_back({{"kind", "region"}, {"sketch", fid}, {"index", idx}});
        } else {
            // Profile: toggle within an effective per-sketch set (whole = all material).
            // Build effective sets for every candidate sketch, toggle the clicked one, re-emit.
            for (const auto& rs : pickRegions_) {
                // effective selected indices for this sketch
                std::vector<int> eff;
                std::vector<int> material;
                for (const auto& pr : rs.regions) {
                    if (pr.isMaterial) material.push_back(pr.index);
                    bool on = explicitHas(rs.fid, pr.index) || (isWhole(rs.fid) && pr.isMaterial);
                    if (on) eff.push_back(pr.index);
                }
                if (rs.fid == fid) {
                    auto it = std::find(eff.begin(), eff.end(), idx);
                    if (it != eff.end()) eff.erase(it); else eff.push_back(idx);
                }
                std::sort(eff.begin(), eff.end());
                // Compact to a whole-sketch pick when eff == the material set (non-empty).
                std::vector<int> mat = material; std::sort(mat.begin(), mat.end());
                if (!eff.empty() && eff == mat) {
                    out.push_back({{"kind", "region"}, {"sketch", rs.fid}});
                } else {
                    for (int i : eff)
                        out.push_back({{"kind", "region"}, {"sketch", rs.fid}, {"index", i}});
                }
            }
            // Never leave a profile empty (would dangle) — fall back to whole clicked sketch.
            if (out.empty())
                out.push_back({{"kind", "region"}, {"sketch", fid}});
        }
        pm->setProfilePicks(st, out);
    } else if (hovS >= 0 && canHover) {
        st.requestCursor(ui::sketch::CursorKind::Hand);
    }
    return hovS >= 0;   // consumed the click target this frame
}

// ---------------------------------------------------------------------------
// datum-plane picker for feature PM wells (reference axis / plane reference geometry)
// ---------------------------------------------------------------------------

// Shared hit-test: user-defined datum PLANES (docMirror refDatums kind=="plane") under the cursor.
// Builds each datum plane's quad EXACTLY as Renderer::drawRefDatums does (origin ± xdir/ydir*ps,
// ps = cam.radius*0.55) and barycentric depth-tests it, so the clickable area coincides with the
// drawn quad. Returns the FRONTMOST hit's feature id + name + camera-distance² (so callers can
// compare it against the three origin-plane depths and pick the single frontmost target), plus the
// winning quad's screen corners for a hover tint. This is what lets the user sketch on / reference
// their OWN datum planes, not just front/top/right.
static bool hitTestDatumPlane(SketchHost& st, ImVec2 mouse, const ImVec2& imgMin, int vw, int vh,
                              std::string& outFid, std::string& outName, float& outDepth,
                              ImVec2 outQuad[4]) {
    outDepth = 1e30f;
    if (!st.renderer || !st.docMirror().hasDoc()) return false;
    const nlohmann::json& datums = st.docMirror().refDatums();
    if (!datums.is_array() || datums.empty()) return false;
    const Renderer& r = *st.renderer;
    const auto& cam = r.camera();
    const float ps = cam.radius * 0.55f;   // matches drawRefDatums datum-plane half-extent
    auto v3 = [](const nlohmann::json& a, Vec3 dflt) -> Vec3 {
        if (a.is_array() && a.size() == 3 && a[0].is_number())
            return Vec3((float)a[0].get<double>(), (float)a[1].get<double>(), (float)a[2].get<double>());
        return dflt;
    };
    auto edge = [](ImVec2 p, ImVec2 q, ImVec2 rr) {
        return (p.x - rr.x) * (q.y - rr.y) - (q.x - rr.x) * (p.y - rr.y);
    };
    static const float uv[4][2] = {{-1, -1}, {1, -1}, {1, 1}, {-1, 1}};
    bool found = false;
    for (const auto& d : datums) {
        if (!d.is_object() || d.value("kind", std::string()) != "plane") continue;
        if (st.hiddenFeatures.count(d.value("id", std::string()))) continue;  // hidden datum: not pickable
        Vec3 o  = v3(d.value("origin", nlohmann::json()), Vec3(0, 0, 0));
        Vec3 xd = v3(d.value("x_dir",  nlohmann::json()), Vec3(1, 0, 0));
        Vec3 yd = v3(d.value("y_dir",  nlohmann::json()), Vec3(0, 1, 0));
        Vec3 c4[4]; ImVec2 s4[4]; bool ok = true;
        for (int i = 0; i < 4; ++i) {
            c4[i] = o + xd * (uv[i][0] * ps) + yd * (uv[i][1] * ps);
            if (!worldToScreen(cam, imgMin, vw, vh, c4[i].x, c4[i].y, c4[i].z, s4[i])) { ok = false; break; }
        }
        if (!ok) continue;
        auto hit = [&](int a, int b, int cc, float& dp) -> bool {
            float area = edge(s4[a], s4[b], s4[cc]);
            if (area == 0.0f) return false;
            float w0 = edge(mouse, s4[b], s4[cc]) / area;
            float w1 = edge(mouse, s4[cc], s4[a]) / area;
            float w2 = edge(mouse, s4[a], s4[b]) / area;
            if (w0 < 0.0f || w1 < 0.0f || w2 < 0.0f) return false;
            Vec3 wp(w0 * c4[a].x + w1 * c4[b].x + w2 * c4[cc].x,
                    w0 * c4[a].y + w1 * c4[b].y + w2 * c4[cc].y,
                    w0 * c4[a].z + w1 * c4[b].z + w2 * c4[cc].z);
            float dx = wp.x - cam.eye.x, dy = wp.y - cam.eye.y, dz = wp.z - cam.eye.z;
            dp = dx * dx + dy * dy + dz * dz;
            return true;
        };
        float dp;
        if ((hit(0, 1, 2, dp) || hit(0, 2, 3, dp)) && dp < outDepth) {
            outDepth = dp;
            outFid  = d.value("id", std::string());
            outName = d.value("name", std::string());
            for (int i = 0; i < 4; ++i) outQuad[i] = s4[i];
            found = true;
        }
    }
    return found;
}

// Hit-test the three default origin planes (front=0/top=1/right=2) that are currently SHOWN, the
// same quads Renderer::planeQuad draws. Returns the frontmost-under-cursor plane's index (+ its
// reserved selector "plane.front/top/right", display name, camera-depth, and screen quad). Lets the
// general viewport selection treat an origin plane like any other pickable so it can be preselected
// for a sketch/feature. Only SHOWN planes are tested, so hidden origin planes never shadow model
// picks during normal modeling.
static bool hitTestOriginPlane(SketchHost& st, ImVec2 mouse, const ImVec2& imgMin, int vw, int vh,
                               std::string& outSel, std::string& outName, float& outDepth,
                               Vec3 outCorners[4]) {
    outDepth = 1e30f;
    if (!st.renderer) return false;
    const Renderer& r = *st.renderer;
    const auto& cam = r.camera();
    Vec3 ro, rd;
    if (!screenRay(cam, imgMin, vw, vh, mouse, ro, rd)) return false;
    static const char* kSel[3]  = {"plane.front", "plane.top", "plane.right"};
    static const char* kName[3] = {"Front Plane", "Top Plane", "Right Plane"};
    bool found = false;
    for (int p = 0; p < 3; ++p) {
        if (!st.showPlane[p]) continue;   // only a visible plane is clickable in the graphics area
        Vec3 c4[4];
        if (!r.planeQuad(p, c4)) continue;
        // Ray vs. the plane of the quad, then a bounds check in the quad's own (u,v) frame. World-
        // space so a quad with corners behind the camera (large origin planes) is still hittable.
        Vec3 uE = c4[1] - c4[0], vE = c4[3] - c4[0];
        Vec3 n  = cross(uE, vE);
        float denom = dot(n, rd);
        if (std::fabs(denom) < 1e-9f) continue;       // ray parallel to plane
        float t = dot(n, c4[0] - ro) / denom;
        if (t <= 0.0f) continue;                       // behind the camera
        Vec3 hit = ro + rd * t;
        Vec3 d = hit - c4[0];
        float uLen = length(uE), vLen = length(vE);
        if (uLen < 1e-9f || vLen < 1e-9f) continue;
        float u = dot(d, uE) / (uLen * uLen);          // normalized 0..1 across the quad
        float v = dot(d, vE) / (vLen * vLen);
        if (u < 0.0f || u > 1.0f || v < 0.0f || v > 1.0f) continue;
        // Squared distance from the camera eye — same metric hitTestDatumPlane returns, so the
        // caller can pick the frontmost across created + origin planes.
        Vec3 he = hit - cam.eye;
        float dp = dot(he, he);
        if (dp < outDepth) {
            outDepth = dp;
            outSel  = kSel[p];
            outName = kName[p];
            for (int i = 0; i < 4; ++i) outCorners[i] = c4[i];
            found = true;
        }
    }
    return found;
}

bool SketchMode::drawPlanePicker(SketchHost& st, ImGuiIO& io, const ImVec2& imgMin,
                                 const ImVec2& imgMax, int vw, int vh, bool imgHovered) {
    pmPlaneHover_ = -1;
    if (active_) return false;                  // editing a sketch: that overlay owns the canvas
    auto* pm = st.propertyManager;
    if (!st.renderer || !st.workerStarted() || !st.docMirror().hasDoc() ||
        !pm || !pm->active() || !pm->activeWellWantsPlane())
        return false;

    const Renderer& r = *st.renderer;
    const auto& cam = r.camera();
    ImDrawList* wdl = ImGui::GetWindowDrawList();
    wdl->PushClipRect(imgMin, imgMax, true);
    const auto& pal = skPal;

    // Project + hit-test the three origin-plane quads exactly as the new-sketch plane pick does
    // (Renderer::planeQuad geometry, frontmost barycentric depth test) so the highlighted plane and
    // the drawn plane always coincide and the overlapping origin planes resolve to the one nearest
    // the camera under the cursor (SolidWorks behavior). See drawOverlay's plane-pick step.
    const char* planeSel[3] = {"plane.front", "plane.top", "plane.right"};
    int hovered = -1;
    float hoveredDepth = 1e30f;
    ImVec2 quadS[3][4];
    bool quadVis[3] = {false, false, false};
    Vec3 quadW[3][4];
    for (int p = 0; p < 3; ++p) {
        Vec3 c4[4];
        if (!r.planeQuad(p, c4)) continue;
        for (int i = 0; i < 4; ++i) quadW[p][i] = c4[i];
        bool ok = true;
        for (int i = 0; i < 4; ++i)
            if (!worldToScreen(cam, imgMin, vw, vh, c4[i].x, c4[i].y, c4[i].z, quadS[p][i])) { ok = false; break; }
        quadVis[p] = ok;
        if (!ok || !imgHovered) continue;
        auto hitDepth = [&](ImVec2 P, int a, int b, int cc, float& depthOut) -> bool {
            ImVec2 sa = quadS[p][a], sb = quadS[p][b], sc = quadS[p][cc];
            auto edge = [](ImVec2 pp, ImVec2 qq, ImVec2 rr) {
                return (pp.x - rr.x) * (qq.y - rr.y) - (qq.x - rr.x) * (pp.y - rr.y);
            };
            float area = edge(sa, sb, sc);
            if (area == 0.0f) return false;
            float w0 = edge(P, sb, sc) / area;
            float w1 = edge(P, sc, sa) / area;
            float w2 = edge(P, sa, sb) / area;
            if (w0 < 0.0f || w1 < 0.0f || w2 < 0.0f) return false;
            Vec3 Wa = quadW[p][a], Wb = quadW[p][b], Wc = quadW[p][cc];
            Vec3 wp(w0 * Wa.x + w1 * Wb.x + w2 * Wc.x,
                    w0 * Wa.y + w1 * Wb.y + w2 * Wc.y,
                    w0 * Wa.z + w1 * Wb.z + w2 * Wc.z);
            float dx = wp.x - cam.eye.x, dy = wp.y - cam.eye.y, dz = wp.z - cam.eye.z;
            depthOut = dx * dx + dy * dy + dz * dz;
            return true;
        };
        float d;
        if ((hitDepth(io.MousePos, 0, 1, 2, d) || hitDepth(io.MousePos, 0, 2, 3, d)) &&
            d < hoveredDepth) {
            hoveredDepth = d;
            hovered = p;
        }
    }
    // Also hit-test the user's OWN datum planes; the frontmost target (origin OR datum plane) wins,
    // so a created plane in front occludes an origin plane behind it, SolidWorks-style.
    std::string datumFid, datumName; float datumDepth = 1e30f; ImVec2 datumQuad[4];
    bool datumHit = imgHovered && hitTestDatumPlane(st, io.MousePos, imgMin, vw, vh,
                                                    datumFid, datumName, datumDepth, datumQuad);
    bool datumWins = datumHit && datumDepth < hoveredDepth;
    if (datumWins) hovered = -1;   // a datum plane is in front; drop the origin-plane emphasis
    pmPlaneHover_ = hovered;
    // Hover tint on the one plane under the cursor (the renderer already draws the three planes +
    // emphasis brightening; viewport_panel feeds pmPlaneHover() as the emphasis index).
    if (datumWins) {
        wdl->AddQuadFilled(datumQuad[0], datumQuad[1], datumQuad[2], datumQuad[3], col(pal.accent, 0.16f));
        wdl->AddQuad(datumQuad[0], datumQuad[1], datumQuad[2], datumQuad[3], col(pal.accent, 0.90f), 2.0f);
    } else if (hovered >= 0 && quadVis[hovered]) {
        wdl->AddQuadFilled(quadS[hovered][0], quadS[hovered][1], quadS[hovered][2],
                           quadS[hovered][3], col(pal.accent, 0.16f));
        wdl->AddQuad(quadS[hovered][0], quadS[hovered][1], quadS[hovered][2],
                     quadS[hovered][3], col(pal.accent, 0.90f), 2.0f);
    }
    // Compact hint pill (below the heads-up view toolbar, same slot as the new-sketch banner).
    const char* banner = "Click a reference plane (incl. your datum planes) \xE2\x80\x94 or a planar face";
    ImVec2 bts = ImGui::CalcTextSize(banner);
    float bcx = (imgMin.x + imgMax.x) * 0.5f;
    ImVec2 b0(bcx - bts.x * 0.5f - 14, imgMin.y + 52.0f * ui::sketch::uiScale());
    ImVec2 b1(bcx + bts.x * 0.5f + 14, b0.y + bts.y + 12);
    wdl->AddRectFilled(b0, b1, col(pal.overlayBg), 6.0f);
    wdl->AddRect(b0, b1, col(pal.overlayBorder), 6.0f);
    wdl->AddText(ImVec2(b0.x + 14, b0.y + 6), col(pal.overlayText), banner);
    wdl->PopClipRect();

    // Click a plane -> route its datum ref into the active well (a created datum plane routes its
    // feature id + name; an origin plane its reserved string). Returning true tells the viewport
    // this frame's click landed on a plane, so model picking doesn't also fire.
    if ((datumWins || hovered >= 0) && imgHovered) {
        st.requestCursor(ui::sketch::CursorKind::Hand);
        if (ImGui::IsMouseClicked(ImGuiMouseButton_Left)) {
            if (datumWins) pm->routeDatumPlane(datumFid, datumName);
            else           pm->routeDatumPlane(planeSel[hovered]);
        }
    }
    return hovered >= 0 || datumWins;
}

// ---------------------------------------------------------------------------
// datum-axis picker for feature PM wells (revolve / circular-pattern axis)
// ---------------------------------------------------------------------------

// Hit-test the displayed datum-axis lines (docMirror::refDatums kind=="axis") in screen space.
// Returns true + the hovered axis's screen endpoints + feature id/name when the cursor is within the
// pick radius. Shared by the PM axis picker (route into a well) and general datum selection. The
// endpoints use L = radius*1.6 so the clickable segment coincides with the drawn dashed amber line.
static bool hitTestDatumAxis(SketchHost& st, ImVec2 mouse, const ImVec2& imgMin, int vw, int vh,
                             ImVec2& outA, ImVec2& outB, std::string& outFid, std::string& outName) {
    if (!st.renderer || !st.docMirror().hasDoc()) return false;
    const nlohmann::json& datums = st.docMirror().refDatums();
    if (!datums.is_array() || datums.empty()) return false;
    const Renderer& r = *st.renderer;
    const auto& cam = r.camera();
    // Match the DRAWN axis length exactly: drawRefDatums uses e=(radius_>0?radius_:kEmptySceneRadius)
    // and L=e*1.6. cam.radius is published as that same sceneR (renderer.cpp drawCurrent), so use it
    // directly — modelRadius() is 0 with no solid body (e.g. during Revolve: sketch + axis, no body),
    // which shrank the pickable segment to ~1.6mm while the visible line spanned ~56mm. See handoff.
    const float L = cam.radius * 1.6f;
    auto distToSeg = [](ImVec2 p, ImVec2 a, ImVec2 b) -> float {
        float vx = b.x - a.x, vy = b.y - a.y, wx = p.x - a.x, wy = p.y - a.y;
        float len2 = vx * vx + vy * vy;
        float t = len2 > 1e-6f ? (wx * vx + wy * vy) / len2 : 0.0f;
        t = t < 0.0f ? 0.0f : (t > 1.0f ? 1.0f : t);
        float dx = a.x + t * vx - p.x, dy = a.y + t * vy - p.y;
        return std::sqrt(dx * dx + dy * dy);
    };
    float best = 9.0f * ui::sketch::uiScale();   // pixel pick radius around the line
    bool found = false;
    for (const auto& d : datums) {
        if (!d.is_object() || d.value("kind", std::string()) != "axis") continue;
        if (st.hiddenFeatures.count(d.value("id", std::string()))) continue;  // hidden datum: not pickable
        const auto& pj = d.contains("point") ? d["point"] : nlohmann::json();
        const auto& dj = d.contains("dir") ? d["dir"] : nlohmann::json();
        if (!pj.is_array() || pj.size() < 3 || !dj.is_array() || dj.size() < 3) continue;
        Vec3 P((float)pj[0].get<double>(), (float)pj[1].get<double>(), (float)pj[2].get<double>());
        Vec3 D((float)dj[0].get<double>(), (float)dj[1].get<double>(), (float)dj[2].get<double>());
        ImVec2 sa, sb;
        if (!worldToScreen(cam, imgMin, vw, vh, P.x - D.x * L, P.y - D.y * L, P.z - D.z * L, sa)) continue;
        if (!worldToScreen(cam, imgMin, vw, vh, P.x + D.x * L, P.y + D.y * L, P.z + D.z * L, sb)) continue;
        float dist = distToSeg(mouse, sa, sb);
        if (dist < best) {
            best = dist; found = true; outA = sa; outB = sb;
            outFid = d.value("id", std::string());
            outName = d.value("name", outFid);
        }
    }
    return found;
}

bool SketchMode::drawAxisPicker(SketchHost& st, ImGuiIO& io, const ImVec2& imgMin,
                                const ImVec2& imgMax, int vw, int vh, bool imgHovered) {
    if (active_) return false;                  // editing a sketch: that overlay owns the canvas
    auto* pm = st.propertyManager;
    if (!st.workerStarted() || !pm || !pm->active() || !pm->activeWellWantsAxis() || !imgHovered)
        return false;
    ImVec2 a, b; std::string fid, name;
    if (!hitTestDatumAxis(st, io.MousePos, imgMin, vw, vh, a, b, fid, name)) return false;

    const auto& pal = skPal;
    ImDrawList* wdl = ImGui::GetWindowDrawList();
    wdl->PushClipRect(imgMin, imgMax, true);
    wdl->AddLine(a, b, col(pal.accent, 0.95f), 4.0f);
    const char* banner = "Click the reference axis";
    ImVec2 bts = ImGui::CalcTextSize(banner);
    float bcx = (imgMin.x + imgMax.x) * 0.5f;
    ImVec2 b0(bcx - bts.x * 0.5f - 14, imgMin.y + 52.0f * ui::sketch::uiScale());
    ImVec2 b1(bcx + bts.x * 0.5f + 14, b0.y + bts.y + 12);
    wdl->AddRectFilled(b0, b1, col(pal.overlayBg), 6.0f);
    wdl->AddRect(b0, b1, col(pal.overlayBorder), 6.0f);
    wdl->AddText(ImVec2(b0.x + 14, b0.y + 6), col(pal.overlayText), banner);
    wdl->PopClipRect();

    st.requestCursor(ui::sketch::CursorKind::Hand);
    if (ImGui::IsMouseClicked(ImGuiMouseButton_Left)) pm->routeDatumAxis(fid, name);
    return true;
}

bool SketchMode::drawCurvePathPicker(SketchHost& st, ImGuiIO& io, const ImVec2& imgMin,
                                     const ImVec2& imgMax, int vw, int vh, bool imgHovered) {
    if (active_) return false;                  // editing a sketch: that overlay owns the canvas
    auto* pm = st.propertyManager;
    if (!st.renderer || !st.workerStarted() || !pm || !pm->active() ||
        !pm->activeWellWantsPathFeature() || !imgHovered)
        return false;
    const auto& cam = st.renderer->camera();
    const ImVec2 mouse = io.MousePos;

    auto distToSeg = [](ImVec2 p, ImVec2 a, ImVec2 b) -> float {
        float vx = b.x - a.x, vy = b.y - a.y, wx = p.x - a.x, wy = p.y - a.y;
        float len2 = vx * vx + vy * vy;
        float t = len2 > 1e-6f ? (wx * vx + wy * vy) / len2 : 0.0f;
        t = t < 0.0f ? 0.0f : (t > 1.0f ? 1.0f : t);
        float dx = a.x + t * vx - p.x, dy = a.y + t * vy - p.y;
        return std::sqrt(dx * dx + dy * dy);
    };
    float best = 9.0f * ui::sketch::uiScale();   // pixel grab radius
    bool found = false, bestIsCurve = false;
    std::string bestFid, bestName;
    std::vector<std::pair<ImVec2, ImVec2>> bestSegs;   // screen segments to highlight

    // --- 1) curve datums (helix / spiral / spline) -------------------------------
    const nlohmann::json& datums = st.docMirror().refDatums();
    if (datums.is_array()) {
        for (const auto& d : datums) {
            if (!d.is_object() || d.value("kind", std::string()) != "curve") continue;
            const auto& pts = d.contains("points") ? d["points"] : nlohmann::json();
            if (!pts.is_array() || pts.size() < 2) continue;
            std::vector<std::pair<ImVec2, ImVec2>> segs;
            float dlocal = 1e9f;
            ImVec2 prev; bool have = false;
            for (const auto& p : pts) {
                ImVec2 s; bool ok = p.is_array() && p.size() >= 3 &&
                    worldToScreen(cam, imgMin, vw, vh, (float)p[0].get<double>(),
                                  (float)p[1].get<double>(), (float)p[2].get<double>(), s);
                if (!ok) { have = false; continue; }
                if (have) { segs.push_back({prev, s}); dlocal = std::min(dlocal, distToSeg(mouse, prev, s)); }
                prev = s; have = true;
            }
            if (dlocal < best) {
                best = dlocal; found = true; bestIsCurve = true;
                bestFid = d.value("id", std::string());
                bestName = d.value("name", bestFid);
                bestSegs = std::move(segs);
            }
        }
    }

    // --- 2) ghost sketches (a whole SKETCH used as the path) ---------------------
    // The sketch the user drew for the path is rendered as a ghost (drawGhosts); hit-test
    // its projected entities so a viewport click on the sketch fills the path well. Skip the
    // sketch already consumed as the Profile so the user can't accidentally self-sweep.
    std::set<std::string> profileSketches;
    for (const auto& sel : pm->profilePicks())
        if (sel.is_object() && sel.contains("sketch") && sel["sketch"].is_string())
            profileSketches.insert(sel["sketch"].get<std::string>());
    auto ghostName = [&](const std::string& fid) -> std::string {
        const nlohmann::json& tree = st.docMirror().tree();
        if (tree.is_object() && tree.contains("features") && tree["features"].is_array())
            for (const auto& tf : tree["features"])
                if (tf.is_object() && tf.value("id", std::string()) == fid)
                    return tf.value("name", fid);
        return fid;
    };
    const float D2R = 3.14159265f / 180.0f;
    for (const auto& g : ghosts_) {
        if (!g.loaded || profileSketches.count(g.fid)) continue;
        auto uv2s = [&](float u, float v, ImVec2& o) -> bool {
            float wx = g.frame[0] * u + g.frame[1] * v + g.frame[3];
            float wy = g.frame[4] * u + g.frame[5] * v + g.frame[7];
            float wz = g.frame[8] * u + g.frame[9] * v + g.frame[11];
            return worldToScreen(cam, imgMin, vw, vh, wx, wy, wz, o);
        };
        std::vector<std::pair<ImVec2, ImVec2>> segs;
        float dlocal = 1e9f;
        auto samplePoly = [&](auto pointAt, int seg) {
            ImVec2 prev; bool have = false;
            for (int i = 0; i <= seg; ++i) {
                float u, v; pointAt(i / (float)seg, u, v);
                ImVec2 p;
                if (!uv2s(u, v, p)) { have = false; continue; }
                if (have) { segs.push_back({prev, p}); dlocal = std::min(dlocal, distToSeg(mouse, prev, p)); }
                prev = p; have = true;
            }
        };
        for (const auto& e : g.ents) {
            if (e.construction) continue;
            if (e.type == "line" || e.type == "slot" || e.type == "arc_slot") {
                samplePoly([&](float t, float& u, float& v) {
                    u = e.p1[0] + (e.p2[0] - e.p1[0]) * t;
                    v = e.p1[1] + (e.p2[1] - e.p1[1]) * t;
                }, 1);
            } else if (e.type == "circle") {
                samplePoly([&](float t, float& u, float& v) {
                    float a = t * 6.2831853f;
                    u = e.center[0] + e.radius * std::cos(a);
                    v = e.center[1] + e.radius * std::sin(a);
                }, 48);
            } else if (e.type == "arc") {
                float a0 = e.start, a1 = e.end;
                if (!e.ccw) { float t = a0; a0 = a1; a1 = t; }
                if (a1 < a0) a1 += 360.0f;
                samplePoly([&](float t, float& u, float& v) {
                    float a = (a0 + (a1 - a0) * t) * D2R;
                    u = e.center[0] + e.radius * std::cos(a);
                    v = e.center[1] + e.radius * std::sin(a);
                }, 40);
            } else if (e.type == "ellipse") {
                float ca = std::cos(e.rot * D2R), sa = std::sin(e.rot * D2R);
                samplePoly([&](float t, float& u, float& v) {
                    float a = t * 6.2831853f;
                    float lx = e.rx * std::cos(a), ly = e.ry * std::sin(a);
                    u = e.center[0] + lx * ca - ly * sa;
                    v = e.center[1] + lx * sa + ly * ca;
                }, 48);
            } else if ((e.type == "spline" || e.type == "style_spline") && e.pts.size() >= 2) {
                for (size_t i = 1; i < e.pts.size(); ++i) {
                    ImVec2 a, b;
                    if (uv2s(e.pts[i - 1].x, e.pts[i - 1].y, a) && uv2s(e.pts[i].x, e.pts[i].y, b)) {
                        segs.push_back({a, b}); dlocal = std::min(dlocal, distToSeg(mouse, a, b));
                    }
                }
            }
        }
        if (dlocal < best) {
            best = dlocal; found = true; bestIsCurve = false;
            bestFid = g.fid; bestName = ghostName(g.fid);
            bestSegs = std::move(segs);
        }
    }
    if (!found) return false;

    const auto& pal = skPal;
    ImDrawList* wdl = ImGui::GetWindowDrawList();
    wdl->PushClipRect(imgMin, imgMax, true);
    for (const auto& s : bestSegs)
        wdl->AddLine(s.first, s.second, col(pal.accent, 0.95f), 4.0f);
    const char* banner = "Click to set this as the sweep path";
    ImVec2 bts = ImGui::CalcTextSize(banner);
    float bcx = (imgMin.x + imgMax.x) * 0.5f;
    ImVec2 b0(bcx - bts.x * 0.5f - 14, imgMin.y + 52.0f * ui::sketch::uiScale());
    ImVec2 b1(bcx + bts.x * 0.5f + 14, b0.y + bts.y + 12);
    wdl->AddRectFilled(b0, b1, col(pal.overlayBg), 6.0f);
    wdl->AddRect(b0, b1, col(pal.overlayBorder), 6.0f);
    wdl->AddText(ImVec2(b0.x + 14, b0.y + 6), col(pal.overlayText), banner);
    wdl->PopClipRect();

    st.requestCursor(ui::sketch::CursorKind::Hand);
    if (ImGui::IsMouseClicked(ImGuiMouseButton_Left))
        pm->routePathFeature(bestFid, bestName, bestIsCurve);
    return true;
}

bool SketchMode::drawDatumSelect(SketchHost& st, ImGuiIO& io, const ImVec2& imgMin,
                                 const ImVec2& imgMax, int vw, int vh, bool imgHovered) {
    if (!st.renderer || active_ || !imgHovered) return false;
    auto* pm = st.propertyManager;
    // A PM picker (axis/plane well) owns datum picks while open — don't also general-select.
    if (pm && pm->active() && (pm->activeWellWantsAxis() || pm->activeWellWantsPlane())) return false;

    ImDrawList* wdl = ImGui::GetWindowDrawList();
    std::string fid, name;

    // A datum AXIS is a thin target, so it wins when the cursor is on the line (a big datum plane
    // behind it would otherwise swallow the click). Fall back to a created datum PLANE so a plane
    // can be selected in the viewport and then preselected for a sketch/feature (SolidWorks pick).
    ImVec2 a, b;
    bool axisHit = hitTestDatumAxis(st, io.MousePos, imgMin, vw, vh, a, b, fid, name);
    if (axisHit) {
        wdl->PushClipRect(imgMin, imgMax, true);
        wdl->AddLine(a, b, col(skPal.accent, 0.90f), 4.0f);
        wdl->PopClipRect();
    } else {
        // Pick the frontmost plane under the cursor across BOTH created datum planes and the (shown)
        // default origin planes, so either can be selected in the graphics area and preselected for a
        // sketch/feature. selectedFeatureId is the created plane's feature id or a reserved
        // "plane.front/top/right" token — both resolve as a sketch/feature plane.
        float cDepth = 1e30f, oDepth = 1e30f;
        ImVec2 cQuad[4]; Vec3 oCorners[4];
        std::string cFid, cName, oSel, oName;
        bool cHit = hitTestDatumPlane(st, io.MousePos, imgMin, vw, vh, cFid, cName, cDepth, cQuad) &&
                    !cFid.empty();
        bool oHit = hitTestOriginPlane(st, io.MousePos, imgMin, vw, vh, oSel, oName, oDepth, oCorners);
        if (!cHit && !oHit) return false;
        const bool originWins = oHit && (!cHit || oDepth < cDepth);
        fid  = originWins ? oSel  : cFid;
        name = originWins ? oName : cName;
        wdl->PushClipRect(imgMin, imgMax, true);
        if (originWins) {
            // Project the origin-plane corners for the outline; skip any edge with an endpoint
            // behind the camera (large quads can wrap) so we never draw a stray line.
            const auto& cam = st.renderer->camera();
            ImVec2 s[4]; bool ok[4];
            for (int i = 0; i < 4; ++i)
                ok[i] = worldToScreen(cam, imgMin, vw, vh, oCorners[i].x, oCorners[i].y,
                                      oCorners[i].z, s[i]);
            for (int i = 0; i < 4; ++i) {
                int j = (i + 1) & 3;
                if (ok[i] && ok[j]) wdl->AddLine(s[i], s[j], col(skPal.accent, 0.90f), 2.5f);
            }
        } else {
            for (int i = 0; i < 4; ++i)
                wdl->AddLine(cQuad[i], cQuad[(i + 1) & 3], col(skPal.accent, 0.90f), 2.5f);
        }
        wdl->PopClipRect();
    }

    st.requestCursor(ui::sketch::CursorKind::Hand);
    if (ImGui::IsMouseClicked(ImGuiMouseButton_Left)) {
        if (!io.KeyCtrl) st.selectionClear();   // plain click = single-select the datum/plane
        st.selectedFeatureId = fid;              // tree row highlights; renderer emphasizes the datum
        // Keep the origin-plane tree emphasis in step when a default plane was picked in the viewport.
        st.planeEmphasis = (fid == "plane.front") ? 0 : (fid == "plane.top") ? 1
                          : (fid == "plane.right") ? 2 : st.planeEmphasis;
        st.treeScrollToSelection = true;
    }
    return true;
}

bool SketchMode::sketchOrbitCenter(Vec3& world) const {
    if (!active_) return false;
    // 2D bbox over the sketch entities (plane u,v); empty sketch -> plane origin (u=v=0).
    float mnu = 1e30f, mnv = 1e30f, mxu = -1e30f, mxv = -1e30f;
    bool any = false;
    auto inc = [&](float u, float v) {
        if (u < mnu) mnu = u; if (v < mnv) mnv = v;
        if (u > mxu) mxu = u; if (v > mxv) mxv = v; any = true;
    };
    for (const auto& e : ents_) {
        inc(e.p1[0], e.p1[1]);
        if (e.type == "line" || e.type == "slot" || e.type == "arc_slot") inc(e.p2[0], e.p2[1]);
        if (e.type == "circle" || e.type == "arc" || e.type == "ellipse" || e.type == "arc_slot") {
            float rr = e.radius > 0 ? e.radius : std::fmax(e.rx, e.ry);
            inc(e.center[0] - rr, e.center[1] - rr);
            inc(e.center[0] + rr, e.center[1] + rr);
        }
        for (const auto& p : e.pts) inc(p.x, p.y);
    }
    float cu = any ? (mnu + mxu) * 0.5f : 0.0f;
    float cv = any ? (mnv + mxv) * 0.5f : 0.0f;
    // Lift (cu,cv,0) to world via frame_ (row-major, sketch(u,v,0)->world) — same pattern as buildBasis.
    const float* fr = frame_;
    world.x = fr[0] * cu + fr[1] * cv + fr[3];
    world.y = fr[4] * cu + fr[5] * cv + fr[7];
    world.z = fr[8] * cu + fr[9] * cv + fr[11];
    return true;
}

// ---------------------------------------------------------------------------
// overlay draw + interaction
// ---------------------------------------------------------------------------

void SketchMode::drawOverlay(SketchHost& st, ImGuiIO& io, const ImVec2& imgMin, const ImVec2& imgMax,
                             int vw, int vh, bool imgHovered) {
    if (!st.renderer) return;
    // Sketch dimensions + Modify box follow the document unit system (mm / inch); refreshed each
    // frame so a status-bar units toggle reformats on-canvas dims and the Modify box live.
    g_skIps = st.ips();
    const auto& pal = skPal;

    // ---- C37-D: SolidWorks plane-pick step (runs while !active_, before a sketch exists) ----
    // The viewport keeps calling drawOverlay (and force-shows the origin planes) while
    // st.sketchPlanePickActive is set; we ray-hit-test the three origin-plane quads + planar faces.
    if (planePicking_ && st.sketchPlanePickActive) {
        st.requestCursor(ui::sketch::CursorKind::Crosshair);
        const Renderer& r = *st.renderer;
        const auto& cam = r.camera();
        ImDrawList* wdl = ImGui::GetWindowDrawList();
        wdl->PushClipRect(imgMin, imgMax, true);

        // Project each origin-plane quad (front=0/top=1/right=2). The corners come straight from
        // Renderer::planeQuad — the SAME geometry drawReferencePlanes renders — so the hit-test
        // polygon and the visible plane always coincide (the old mirrored copy used cam.center,
        // which includes the pan offset, and drifted away from the drawn planes after any pan).
        const char* planeSel[3] = {"plane.front", "plane.top", "plane.right"};
        int hovered = -1;
        float hoveredDepth = 1e30f;   // C52: pick the FRONTMOST plane under the cursor (SW behavior)
        ImVec2 quadS[3][4];
        bool quadVis[3] = {false, false, false};
        Vec3 quadW[3][4];
        for (int p = 0; p < 3; ++p) {
            Vec3 c4[4];
            if (!r.planeQuad(p, c4)) continue;
            for (int i = 0; i < 4; ++i) quadW[p][i] = c4[i];
            bool ok = true;
            for (int i = 0; i < 4; ++i) {
                if (!worldToScreen(cam, imgMin, vw, vh, c4[i].x, c4[i].y, c4[i].z, quadS[p][i])) { ok = false; break; }
            }
            quadVis[p] = ok;
            if (!ok || !imgHovered) continue;
            // C52 plane-pick fix: the three origin planes all pass through the origin and overlap
            // heavily on screen, so a simple point-in-quad test that keeps the LAST matching index
            // always resolved the overlap region to Right (YZ, index 2) — the user's "wrong plane"
            // bug. Instead, point-in-triangle with BARYCENTRIC weights so we can interpolate the
            // world-space hit point and compare DEPTHS, choosing the plane whose surface is closest
            // to the camera at this pixel (frontmost) — exactly how SolidWorks highlights the plane
            // under the cursor.
            auto hitDepth = [&](ImVec2 P, int a, int b, int cc, float& depthOut) -> bool {
                ImVec2 sa = quadS[p][a], sb = quadS[p][b], sc = quadS[p][cc];
                auto edge = [](ImVec2 pp, ImVec2 qq, ImVec2 rr) {
                    return (pp.x - rr.x) * (qq.y - rr.y) - (qq.x - rr.x) * (pp.y - rr.y);
                };
                float area = edge(sa, sb, sc);
                if (area == 0.0f) return false;
                float w0 = edge(P, sb, sc) / area;   // weight of vertex a
                float w1 = edge(P, sc, sa) / area;   // weight of vertex b
                float w2 = edge(P, sa, sb) / area;   // weight of vertex c
                // Inside-test is winding-agnostic: dividing by the signed area normalises the sign,
                // so an interior point has all three weights >= 0 for both CW and CCW screen winding.
                if (w0 < 0.0f || w1 < 0.0f || w2 < 0.0f) return false;
                Vec3 Wa = quadW[p][a], Wb = quadW[p][b], Wc = quadW[p][cc];
                Vec3 wp(w0 * Wa.x + w1 * Wb.x + w2 * Wc.x,
                        w0 * Wa.y + w1 * Wb.y + w2 * Wc.y,
                        w0 * Wa.z + w1 * Wb.z + w2 * Wc.z);
                float dx = wp.x - cam.eye.x, dy = wp.y - cam.eye.y, dz = wp.z - cam.eye.z;
                depthOut = dx * dx + dy * dy + dz * dz;
                return true;
            };
            float d;
            if ((hitDepth(io.MousePos, 0, 1, 2, d) || hitDepth(io.MousePos, 0, 2, 3, d)) &&
                d < hoveredDepth) {
                hoveredDepth = d;
                hovered = p;
            }
        }
        // Also hit-test the user's OWN datum planes so a sketch can be started on one (not just the
        // three origin planes). The frontmost target (origin OR datum plane) wins.
        std::string sDatumFid, sDatumName; float sDatumDepth = 1e30f; ImVec2 sDatumQuad[4];
        bool sDatumHit = imgHovered && hitTestDatumPlane(st, io.MousePos, imgMin, vw, vh,
                                                         sDatumFid, sDatumName, sDatumDepth, sDatumQuad);
        bool sDatumWins = sDatumHit && sDatumDepth < hoveredDepth;
        if (sDatumWins) hovered = -1;   // a datum plane is in front; drop origin-plane emphasis
        planeHover_ = hovered;
        // The renderer already draws all three datum planes (fill + outline, with emphasis
        // brightening the hovered one — viewport_panel passes planeHover() as the emphasis).
        // Painting a second translucent fill per plane on top made the overlap muddy and
        // unreadable (user bug report), so the overlay adds ONLY a hover tint on the one
        // plane under the cursor.
        if (sDatumWins) {
            wdl->AddQuadFilled(sDatumQuad[0], sDatumQuad[1], sDatumQuad[2], sDatumQuad[3], col(pal.accent, 0.16f));
            wdl->AddQuad(sDatumQuad[0], sDatumQuad[1], sDatumQuad[2], sDatumQuad[3], col(pal.accent, 0.90f), 2.0f);
        } else if (hovered >= 0 && quadVis[hovered]) {
            wdl->AddQuadFilled(quadS[hovered][0], quadS[hovered][1], quadS[hovered][2],
                               quadS[hovered][3], col(pal.accent, 0.16f));
            wdl->AddQuad(quadS[hovered][0], quadS[hovered][1], quadS[hovered][2],
                         quadS[hovered][3], col(pal.accent, 0.90f), 2.0f);
        }
        // Click: a datum plane wins if frontmost; else an origin plane; else the hovered planar face.
        if (imgHovered && ImGui::IsMouseClicked(ImGuiMouseButton_Left)) {
            if (sDatumWins) {
                // A created datum plane: refs.plane = the bare feature-id STRING (the engine's sketch
                // compiler looks it up in the results map as a PlaneResult — see document.cpp).
                planePicking_ = false; st.sketchPlanePickActive = false;
                consumePlanePick(st, nlohmann::json(sDatumFid));
            } else if (hovered >= 0) {
                planePicking_ = false; st.sketchPlanePickActive = false;
                consumePlanePick(st, planeSel[hovered]);
            } else if (st.hover.valid() && st.hover.kind == worker::PickKind::Face) {
                // Planar-face pick: resolve via the same path enterNewOnSelection uses. We try the
                // hovered face's selector; the engine rejects non-planar faces (toast + stay).
                std::string perr;
                std::string sel = st.docMirror().resolvePick(st.worker(), st.hover.body_id, "face",
                                                           st.hover.topo_id, perr);
                if (!sel.empty()) {
                    nlohmann::json js;
                    try { js = nlohmann::json::parse(sel); } catch (...) {}
                    if (js.is_object()) {
                        std::string newFid = consumePlanePick(st, js);
                        if (newFid.empty())
                            st.toast(ui::sketch::ToastLevel::Warn, "That face can't host a sketch — pick a planar face or plane");
                        else { planePicking_ = false; st.sketchPlanePickActive = false; }
                    }
                }
            }
        }
        // Top-center banner.
        const char* banner = planeEditFid_.empty()
            ? "Select a plane or a planar face for the sketch \xE2\x80\x94 Esc to cancel"
            : "Select the new sketch plane or a planar face \xE2\x80\x94 Esc to cancel";
        ImVec2 bts = ImGui::CalcTextSize(banner);
        float bcx = (imgMin.x + imgMax.x) * 0.5f;
        // Sit BELOW the heads-up view toolbar (centered pill at imgMin.y+8, ~34px tall) so the
        // two don't overlap (the HUD stays visible during plane-pick).
        ImVec2 b0(bcx - bts.x * 0.5f - 14, imgMin.y + 52.0f * ui::sketch::uiScale());
        ImVec2 b1(bcx + bts.x * 0.5f + 14, b0.y + bts.y + 12);
        wdl->AddRectFilled(b0, b1, col(pal.overlayBg), 6.0f);
        wdl->AddRect(b0, b1, col(pal.overlayBorder), 6.0f);
        wdl->AddText(ImVec2(b0.x + 14, b0.y + 6), col(pal.overlayText), banner);
        wdl->PopClipRect();
        // Esc cancels the whole thing (new-sketch pick OR an Edit-Sketch-Plane reparent).
        if (ImGui::IsKeyPressed(ImGuiKey_Escape)) {
            planePicking_ = false; st.sketchPlanePickActive = false;
            bool wasReparent = !planeEditFid_.empty();
            planeEditFid_.clear();
            st.toast(ui::sketch::ToastLevel::Info, wasReparent ? "Edit plane cancelled" : "Sketch cancelled");
        }
        return;
    }

    if (!active_) return;
    buildBasis(*st.renderer, imgMin, vw, vh);
    if (!basisOk_) return;

    ImDrawList* dl = ImGui::GetWindowDrawList();
    dl->PushClipRect(imgMin, imgMax, true);

    // C37-D: crosshair cursor whenever a drawing tool or Smart Dimension is active.
    if (tool_ != SketchTool::None) st.requestCursor(ui::sketch::CursorKind::Crosshair);

    // C37-D: the confirmation corner (top-right) sits over the viewport image, so its rects steal
    // hover from the tools. Compute them here and treat clicks there as NOT hovering the canvas, so a
    // tool/dim click on ✓/✗ doesn't also place a point. (Geometry mirrors the corner block below.)
    {
        const float sz = 30.0f, pad = 12.0f;
        ImVec2 okMin(imgMax.x - pad - sz, imgMin.y + pad), okMax(okMin.x + sz, okMin.y + sz);
        ImVec2 cxMin(okMin.x, okMax.y + 6), cxMax(cxMin.x + sz, cxMin.y + sz);
        if (imgHovered && (ImGui::IsMouseHoveringRect(okMin, okMax) ||
                           ImGui::IsMouseHoveringRect(cxMin, cxMax)))
            imgHovered = false;
    }

    // C37-D / C50: in-sketch undo + redo (the global app handler skips both while a sketch is
    // active). Ctrl+Z undoes (mid-stroke pops the last point); Ctrl+Y or Ctrl+Shift+Z redoes.
    // Suppressed while a text field (the Modify box) owns the keyboard. Returns early if the
    // sketch vanished from the undo/redo.
    if (!io.WantTextInput && io.KeyCtrl) {
        if (io.KeyShift && ImGui::IsKeyPressed(ImGuiKey_Z, false)) {
            inSketchRedo(st);
            if (!active_) { dl->PopClipRect(); return; }
        } else if (ImGui::IsKeyPressed(ImGuiKey_Z, false)) {
            inSketchUndo(st);
            if (!active_) { dl->PopClipRect(); return; }
        } else if (ImGui::IsKeyPressed(ImGuiKey_Y, false)) {
            inSketchRedo(st);
            if (!active_) { dl->PopClipRect(); return; }
        }
    }

    // ---- Wave 4C/4D: sketch entity copy / paste keyboard routing ----
    // Ctrl+C copies the current selection; Ctrl+V arms paste-placement from a SketchEntities
    // clipboard. Suppressed while a text field owns input, while placement is already in progress
    // (Ctrl+V then is a no-op), and (Ctrl+C) only when there's a selection. The Ctrl+X/cut path is
    // intentionally NOT bound here — deleting sketch entities already has Delete; copy+delete on
    // Ctrl+X would surprise. The global app router only forwards Ctrl+C/V to sketch mode.
    if (!io.WantTextInput && io.KeyCtrl && !pastePlacing_) {
        if (ImGui::IsKeyPressed(ImGuiKey_C, false) && !selIds_.empty())
            sketchCopySelection(st);
        else if (ImGui::IsKeyPressed(ImGuiKey_V, false) &&
                 st.clipboard.kind == ui::sketch::SketchClipboard::Kind::SketchEntities)
            sketchPasteBegin(st);
    }

    // ---- Wave 4C/4D: paste-placement mode (preview drawn at the END so the existing geometry still
    // renders underneath). While placing we suppress all tool/selection INTERACTION by treating the
    // canvas as not-hovered, so a placement click never also selects/places — but grid + entities +
    // dimensions still draw normally below. The follow-cursor preview + commit/cancel run after the
    // geometry pass (search "paste-placement preview").
    if (pastePlacing_) imgHovered = false;

    // C41 (T1): Esc consumption flag. The specific Esc behaviors that KEEP the tool armed
    // (Smart Dim pick-buffer clear, Offset stage back-out, Modify box close) set this so the
    // hover-independent catch-all at the end of drawOverlay doesn't also disarm the tool.
    bool escHandled = false;

    // ---- faint grid (lines along sketch u/v through the origin neighbourhood) ----
    // Off by default (SolidWorks ships with the sketch grid off); right-click > Grid toggles it.
    const ImU32 gridCol = col(pal.sketchGrid);
    const ImU32 axisCol = col(pal.sketchGrid, 2.2f);
    if (showGrid_) {
        // Estimate the visible (u,v) span from the four image corners.
        float umin = 1e9f, umax = -1e9f, vmin = 1e9f, vmax = -1e9f;
        ImVec2 corners[4] = {imgMin, ImVec2(imgMax.x, imgMin.y), ImVec2(imgMin.x, imgMax.y), imgMax};
        for (auto& c : corners) {
            float u, v; screenToUV(c, u, v);
            umin = std::fmin(umin, u); umax = std::fmax(umax, u);
            vmin = std::fmin(vmin, v); vmax = std::fmax(vmax, v);
        }
        // Clamp the line count so a zoomed-out view doesn't draw thousands of lines.
        int count = 0;
        float gu0 = std::floor(umin / kGridStep) * kGridStep;
        for (float u = gu0; u <= umax && count < 400; u += kGridStep, ++count) {
            ImVec2 a = uvToScreen(u, vmin), b = uvToScreen(u, vmax);
            dl->AddLine(a, b, std::fabs(u) < 1e-3f ? axisCol : gridCol, std::fabs(u) < 1e-3f ? 1.4f : 1.0f);
        }
        count = 0;
        float gv0 = std::floor(vmin / kGridStep) * kGridStep;
        for (float v = gv0; v <= vmax && count < 400; v += kGridStep, ++count) {
            ImVec2 a = uvToScreen(umin, v), b = uvToScreen(umax, v);
            dl->AddLine(a, b, std::fabs(v) < 1e-3f ? axisCol : gridCol, std::fabs(v) < 1e-3f ? 1.4f : 1.0f);
        }
    }

    // ---- sketch origin marker (always visible, SolidWorks red L-arrows at (0,0)) ----
    // The origin is real, pickable geometry: snapPoint, hitTestHandle and the Smart Dimension hit-
    // test all expose it as the "origin" handle, so entities can be anchored / dimensioned to it and
    // relations added between it and other sketch geometry. When selected it renders in the accent
    // color with a filled selection ring (matching selected entities), red otherwise.
    {
        const bool oSel = isSelected("origin");
        const ImU32 oc = oSel ? col(pal.accent) : col(pal.statusErr, 0.95f);
        const float th = oSel ? 2.6f : 1.8f;
        ImVec2 o = uvToScreen(0, 0);
        // unit u/v directions in screen space (normalized axis deltas)
        auto unit = [](ImVec2 d) {
            float L = std::hypot(d.x, d.y);
            return L > 1e-6f ? ImVec2(d.x / L, d.y / L) : ImVec2(1, 0);
        };
        ImVec2 du = unit(uAxisS_), dv = unit(vAxisS_);
        const float len = 22.0f * ui::sketch::uiScale();
        ImVec2 tu(o.x + du.x * len, o.y + du.y * len);
        ImVec2 tv(o.x + dv.x * len, o.y + dv.y * len);
        if (oSel) dl->AddCircleFilled(o, 5.5f * ui::sketch::uiScale(), col(pal.accent, 0.30f), 16);
        dl->AddLine(o, tu, oc, th);
        dl->AddLine(o, tv, oc, th);
        arrowHead(dl, tu, du.x, du.y, 7.0f, oc);
        arrowHead(dl, tv, dv.x, dv.y, 7.0f, oc);
        dl->AddCircle(o, 3.5f, oc, 12, th);
    }

    // ---- C51: closed-profile region shading (under the entities) ----
    // Loops detected from the live geometry are filled faintly so a closed profile reads as a
    // selectable face; the one the cursor has dwelled in (hotRegion_) brightens + gets an outline,
    // signalling that a click will grab its whole loop. Drawn BEFORE the entities so the boundary
    // lines stay crisp on top. The fill uses ImGui's AddConcavePolyFilled so concave profiles
    // (L-brackets etc.) shade as one clean solid translucent region with no triangle-seam streaks
    // (rebuildRegions() only emits simple, non-self-intersecting loops, which it handles).
    rebuildRegions();
    if (hotRegion_ >= (int)regions_.size()) hotRegion_ = -1;
    {
        const ImU32 fillCol = col(pal.accent, 0.13f);
        const ImU32 hotFill = col(pal.accent, 0.28f);
        const ImU32 hotEdge = col(pal.accent, 0.65f);
        for (size_t ri = 0; ri < regions_.size(); ++ri) {
            const auto& rg = regions_[ri];
            bool hot = (tool_ == SketchTool::None && (int)ri == hotRegion_);
            std::vector<ImVec2> sp; sp.reserve(rg.poly.size());
            for (const auto& p : rg.poly) sp.push_back(uvToScreen(p.x, p.y));
            dl->AddConcavePolyFilled(sp.data(), (int)sp.size(), hot ? hotFill : fillCol);
            if (hot) dl->AddPolyline(sp.data(), (int)sp.size(), hotEdge, ImDrawFlags_Closed, 1.5f);
        }
    }

    // ---- C-bug2: relation badges (geometric / non-dimensional constraints) ----
    // T-BL41: badges are HIDDEN by default; they only render when the SmartDim (dimension/relation)
    // tool is active so the canvas stays uncluttered during normal modelling.
    // Drawn HERE, BENEATH the entity lines, so geometry stays crisp on top (the user complaint was
    // badges sitting on the lines). Three phases: (1) collect each badge's initial chip center plus
    // the geometry point it annotates, (2) relax overlapping chips apart, (3) draw a hairline leader
    // from the geometry to any displaced chip, then the chip. Dimension annotations still render
    // AFTER the entity loop (they read better on top of geometry).
    relHits_.clear();   // always clear so stale hit-rects don't accumulate when badges are hidden
    if (tool_ == SketchTool::SmartDim) {
        const float bs = 16.0f * ui::sketch::uiScale();                 // chip side
        const ImU32 relBg        = col(pal.sketchRelBg);
        const ImU32 relBorder    = col(pal.sketchRelBorder);
        const ImU32 relInk       = col(pal.sketchInk);
        const ImU32 relSelBg     = col(pal.accent, 0.85f);
        const ImU32 relSelBorder = col(pal.accent);
        const ImU32 relSelInk    = col(pal.statusBg);         // dark glyph reads on the accent fill
        const ImU32 dimErrCol    = col(pal.statusErr);        // conflicting/redundant relations -> red
        const ImU32 leaderCol    = col(pal.sketchRelBorder, 0.4f);
        auto isConflict = [&](const std::string& cid) {
            return std::find(conflicting_.begin(), conflicting_.end(), cid) != conflicting_.end()
                || std::find(redundant_.begin(),  redundant_.end(),  cid) != redundant_.end();
        };
        auto baseId = [](const std::string& h) { return h.substr(0, h.find('.')); };
        // Screen point a ref handle resolves to ("e3" entity, "e3.p2" point handle, or "origin").
        auto refScreen = [&](const std::string& h, ImVec2& s) -> bool {
            if (baseId(h) == "origin") { s = uvToScreen(0, 0); return true; }
            const SketchEntity* e = entById(baseId(h));
            if (!e) return false;
            if (h.find(".p1") != std::string::npos)           s = uvToScreen(e->p1[0], e->p1[1]);
            else if (h.find(".p2") != std::string::npos)      s = uvToScreen(e->p2[0], e->p2[1]);
            else if (h.find(".center") != std::string::npos)  s = uvToScreen(e->center[0], e->center[1]);
            else if (e->type == "line")  s = uvToScreen((e->p1[0]+e->p2[0])*0.5f, (e->p1[1]+e->p2[1])*0.5f);
            else                         s = uvToScreen(e->center[0], e->center[1]);
            return true;
        };
        // Geometry point a badge POINTS AT: entity midpoint / the point / arc rim mid-sweep.
        auto refAttach = [&](const std::string& h, ImVec2& s) -> bool {
            if (h.find('.') != std::string::npos || baseId(h) == "origin") return refScreen(h, s);
            const SketchEntity* e = entById(baseId(h));
            if (!e) return false;
            if (e->type == "line" || e->type == "slot") {
                ImVec2 a = uvToScreen(e->p1[0], e->p1[1]), b = uvToScreen(e->p2[0], e->p2[1]);
                s = ImVec2((a.x + b.x) * 0.5f, (a.y + b.y) * 0.5f);
                return true;
            }
            if (e->type == "circle" || e->type == "arc" || e->type == "arc_slot") {
                float aMid = 45.0f;
                if (e->type != "circle") {
                    float a0 = e->start, a1 = e->end; if (a1 < a0) a1 += 360.0f;
                    aMid = (a0 + a1) * 0.5f;
                }
                float ar = aMid * 3.14159265f / 180.0f;
                s = uvToScreen(e->center[0] + e->radius * std::cos(ar),
                               e->center[1] + e->radius * std::sin(ar));
                return true;
            }
            return refScreen(h, s);
        };
        // Initial chip center: the attachment point pushed clear of the geometry line.
        auto badgeAnchor = [&](const std::string& h, ImVec2& out) -> bool {
            const float off = 10.0f * ui::sketch::uiScale() + bs * 0.5f;
            if (h.find('.') != std::string::npos || baseId(h) == "origin") {
                ImVec2 s; if (!refScreen(h, s)) return false;
                out = ImVec2(s.x + off * 0.8f, s.y - off * 0.8f);
                return true;
            }
            const SketchEntity* e = entById(baseId(h));
            if (!e) return false;
            if (e->type == "line" || e->type == "slot") {
                ImVec2 a = uvToScreen(e->p1[0], e->p1[1]), b = uvToScreen(e->p2[0], e->p2[1]);
                ImVec2 m((a.x + b.x) * 0.5f, (a.y + b.y) * 0.5f);
                float dx = b.x - a.x, dy = b.y - a.y; float L = std::hypot(dx, dy);
                float nx = L > 1e-3f ? -dy / L : 0.0f, ny = L > 1e-3f ? dx / L : -1.0f;
                out = ImVec2(m.x + nx * off, m.y + ny * off);
                return true;
            }
            if (e->type == "circle" || e->type == "arc" || e->type == "arc_slot") {
                float aMid = 45.0f;
                if (e->type != "circle") {
                    float a0 = e->start, a1 = e->end; if (a1 < a0) a1 += 360.0f;
                    aMid = (a0 + a1) * 0.5f;
                }
                float ar = aMid * 3.14159265f / 180.0f;
                ImVec2 cc = uvToScreen(e->center[0], e->center[1]);
                ImVec2 rim = uvToScreen(e->center[0] + e->radius * std::cos(ar),
                                        e->center[1] + e->radius * std::sin(ar));
                float dx = rim.x - cc.x, dy = rim.y - cc.y; float L = std::hypot(dx, dy);
                if (L > 1e-3f) out = ImVec2(rim.x + dx / L * off, rim.y + dy / L * off);
                else           out = ImVec2(cc.x + off, cc.y - off);
                return true;
            }
            ImVec2 s; if (!refScreen(h, s)) return false;
            out = ImVec2(s.x + off * 0.8f, s.y - off * 0.8f);
            return true;
        };
        // (1) Collect every non-dimensional relation as a Badge.
        struct Badge { std::string cid; const char* glyph; const char* name; bool conflict;
                       ImVec2 pos, anchor; };
        std::vector<Badge> badges;
        auto pushBadge = [&](const std::string& cid, const std::string& h, const char* glyph,
                             const char* name, bool conflict) {
            ImVec2 pos, anchor;
            if (!badgeAnchor(h, pos)) return;
            if (!refAttach(h, anchor)) anchor = pos;
            badges.push_back({cid, glyph, name, conflict, pos, anchor});
        };
        for (const auto& c : cons_) {
            if (c.isDimensional() || c.refs.empty()) continue;
            const char* g = c.type == "horizontal"    ? "-"
                          : c.type == "vertical"      ? "|"
                          : c.type == "parallel"      ? "//"
                          : c.type == "perpendicular" ? "L"
                          : c.type == "equal"         ? "="
                          : c.type == "coincident"    ? "o"
                          : c.type == "concentric"    ? "c"
                          : c.type == "tangent"       ? "T"
                          : c.type == "collinear"     ? "col"
                          : c.type == "coradial"      ? "cr"
                          : c.type == "midpoint"      ? "m"
                          : c.type == "symmetric"     ? "s"
                          : c.type == "fix"           ? "x" : nullptr;
            if (!g) continue;
            bool conflict = isConflict(c.id);
            // Entity-pair relations get a badge on EACH ref (SolidWorks shows both sides);
            // single-entity / point relations get one badge at the first ref.
            bool pairRel = c.type == "parallel" || c.type == "perpendicular" ||
                           c.type == "equal" || c.type == "tangent" || c.type == "concentric" ||
                           c.type == "collinear" || c.type == "coradial" || c.type == "symmetric";
            if (pairRel) { for (const auto& rh : c.refs) pushBadge(c.id, rh, g, c.type.c_str(), conflict); }
            else         pushBadge(c.id, c.refs[0], g, c.type.c_str(), conflict);
        }
        // (2) Spread: 8 relaxation passes pushing any overlapping pair apart by half the overlap.
        for (int it = 0; it < 8; ++it) {
            for (size_t i = 0; i < badges.size(); ++i) {
                for (size_t j = i + 1; j < badges.size(); ++j) {
                    float dx = badges[j].pos.x - badges[i].pos.x;
                    float dy = badges[j].pos.y - badges[i].pos.y;
                    float d = std::hypot(dx, dy);
                    const float minD = bs + 2.0f;
                    if (d >= minD) continue;
                    float ox, oy;
                    if (d < 1e-3f) { ox = minD * 0.5f; oy = 0.0f; }   // coincident -> shove along +x
                    else { float push = (minD - d) * 0.5f; ox = dx / d * push; oy = dy / d * push; }
                    badges[i].pos.x -= ox; badges[i].pos.y -= oy;
                    badges[j].pos.x += ox; badges[j].pos.y += oy;
                }
            }
        }
        // (3) Draw: hairline leader (only when the chip drifted off its anchor) + chip + hit-rect.
        // Note: relHits_.clear() is now done BEFORE the SmartDim gate above.
        for (const auto& b : badges) {
            const ImVec2 gp = b.pos;
            if (std::hypot(gp.x - b.anchor.x, gp.y - b.anchor.y) > bs)
                dl->AddLine(b.anchor, gp, leaderCol, 0.8f);
            ImVec2 c0(gp.x - bs * 0.5f, gp.y - bs * 0.5f), c1(gp.x + bs * 0.5f, gp.y + bs * 0.5f);
            bool hot = isRelSelected(b.cid) || hoverRelCid_ == b.cid;
            dl->AddRectFilled(c0, c1, hot ? relSelBg : relBg, 3.5f * ui::sketch::uiScale());
            dl->AddRect(c0, c1, b.conflict ? dimErrCol : (hot ? relSelBorder : relBorder),
                        3.5f * ui::sketch::uiScale(), 0, (b.conflict || hot) ? 1.8f : 1.2f);
            ImVec2 gs = ImGui::CalcTextSize(b.glyph);
            dl->AddText(ImVec2(gp.x - gs.x * 0.5f, gp.y - gs.y * 0.5f),
                        b.conflict ? dimErrCol : (hot ? relSelInk : relInk), b.glyph);
            relHits_.push_back({b.cid, c0, c1});
            if (imgHovered && io.MousePos.x >= c0.x && io.MousePos.x <= c1.x &&
                io.MousePos.y >= c0.y && io.MousePos.y <= c1.y)
                ImGui::SetTooltip("%s", b.name);
        }
    }

    // ---- entities (color by solver status via theme roles) ----
    auto statusCol = [&](const SketchEntity& e) -> ImU32 {
        if (e.status == "over") return col(pal.statusErr);
        if (e.status == "full") return col(pal.sketchInk);
        return col(pal.sketchUnder);   // under-defined (blue-ish)
    };
    // Construction dashes: SolidWorks centerline style, dash=6px gap=4px at scale 1.
    const float kConDash = 6.0f * ui::sketch::uiScale();
    const float kConGap  = 4.0f * ui::sketch::uiScale();
    // Stroke one segment — solid for normal geometry, dashed for construction.
    // addSeg accumulates points into a local vector so the polyline dash phase is continuous.
    for (const auto& e : ents_) {
        // C41 (T2): selected entities render in the accent color, slightly thicker.
        bool sel = !selIds_.empty() && isSelected(e.id);
        ImU32 cc = sel ? col(pal.accent) : statusCol(e);
        float th = (e.construction ? 1.0f : 2.0f) + (sel ? 1.0f : 0.0f);
        // Helper: stroke a polyline; uses dashed rendering for construction entities.
        auto strokePoly = [&](const std::vector<ImVec2>& pts) {
            if (e.construction) {
                drawDashedPolyline(dl, pts, cc, th, kConDash, kConGap);
            } else {
                for (size_t i = 1; i < pts.size(); ++i)
                    dl->AddLine(pts[i-1], pts[i], cc, th);
            }
        };
        if (e.type == "line") {
            ImVec2 a = uvToScreen(e.p1[0], e.p1[1]), b = uvToScreen(e.p2[0], e.p2[1]);
            if (e.construction) {
                drawDashedLine(dl, a, b, cc, th, kConDash, kConGap);
            } else {
                dl->AddLine(a, b, cc, th);
            }
            dl->AddCircleFilled(a, 3.0f, cc);
            dl->AddCircleFilled(b, 3.0f, cc);
        } else if (e.type == "circle") {
            // Sample the circle in (u,v) so it follows the plane mapping exactly.
            int seg = 64;
            std::vector<ImVec2> pts; pts.reserve(seg + 1);
            for (int i = 0; i <= seg; ++i) {
                float a = i / (float)seg * 6.2831853f;
                pts.push_back(uvToScreen(e.center[0] + e.radius * std::cos(a),
                                         e.center[1] + e.radius * std::sin(a)));
            }
            strokePoly(pts);
            dl->AddCircleFilled(uvToScreen(e.center[0], e.center[1]), 2.5f, cc);
        } else if (e.type == "arc") {
            float a0 = e.start, a1 = e.end;
            if (!e.ccw) { float t = a0; a0 = a1; a1 = t; }
            if (a1 < a0) a1 += 360.0f;
            int seg = 48;
            std::vector<ImVec2> pts; pts.reserve(seg + 1);
            for (int i = 0; i <= seg; ++i) {
                float a = (a0 + (a1 - a0) * (i / (float)seg)) * 3.14159265f / 180.0f;
                pts.push_back(uvToScreen(e.center[0] + e.radius * std::cos(a),
                                         e.center[1] + e.radius * std::sin(a)));
            }
            strokePoly(pts);
        } else if (e.type == "slot") {
            // Closed capsule profile: two side lines tangent to the caps (parallel to the spine,
            // offset by the half-width = end radius) plus a trimmed semicircle arc at each end.
            // The spine itself renders as a construction centerline, and the trimmed-away inner
            // halves of each end circle render as construction too (as if trimmed by hand).
            float dx = e.p2[0] - e.p1[0], dy = e.p2[1] - e.p1[1];
            float L = std::hypot(dx, dy);
            float hw = e.width * 0.5f;
            float nx = L > 1e-6f ? -dy / L : 0.0f, ny = L > 1e-6f ? dx / L : 0.0f;
            ImVec2 s1a = uvToScreen(e.p1[0] + nx * hw, e.p1[1] + ny * hw);
            ImVec2 s1b = uvToScreen(e.p2[0] + nx * hw, e.p2[1] + ny * hw);
            ImVec2 s2a = uvToScreen(e.p1[0] - nx * hw, e.p1[1] - ny * hw);
            ImVec2 s2b = uvToScreen(e.p2[0] - nx * hw, e.p2[1] - ny * hw);
            float axis1 = std::atan2(e.p1[1] - e.p2[1], e.p1[0] - e.p2[0]);  // outward at p1
            float axis2 = std::atan2(e.p2[1] - e.p1[1], e.p2[0] - e.p1[0]);  // outward at p2
            // Sample a half-circle arc (180 deg centered on `axis`) around end `c`.
            auto halfArc = [&](const float* c, float axis) {
                std::vector<ImVec2> pts; pts.reserve(25);
                for (int i = 0; i <= 24; ++i) {
                    float ang = axis - 1.5707963f + 3.14159265f * (i / 24.0f);
                    pts.push_back(uvToScreen(c[0] + hw * std::cos(ang), c[1] + hw * std::sin(ang)));
                }
                return pts;
            };
            ImVec2 a = uvToScreen(e.p1[0], e.p1[1]), b = uvToScreen(e.p2[0], e.p2[1]);
            if (e.construction) {
                drawDashedLine(dl, s1a, s1b, cc, th, kConDash, kConGap);
                drawDashedLine(dl, s2a, s2b, cc, th, kConDash, kConGap);
                drawDashedPolyline(dl, halfArc(e.p1, axis1), cc, th, kConDash, kConGap);
                drawDashedPolyline(dl, halfArc(e.p2, axis2), cc, th, kConDash, kConGap);
            } else {
                dl->AddLine(s1a, s1b, cc, th);
                dl->AddLine(s2a, s2b, cc, th);
                strokePoly(halfArc(e.p1, axis1));   // outer (kept) cap halves
                strokePoly(halfArc(e.p2, axis2));
                // construction: centerline spine + the trimmed-away inner half of each end circle
                const ImU32 conCol = sel ? cc : col(pal.sketchUnder, 0.85f);
                drawDashedLine(dl, a, b, conCol, 1.0f, kConDash, kConGap);
                drawDashedPolyline(dl, halfArc(e.p1, axis1 + 3.14159265f), conCol, 1.0f,
                                   kConDash, kConGap);
                drawDashedPolyline(dl, halfArc(e.p2, axis2 + 3.14159265f), conCol, 1.0f,
                                   kConDash, kConGap);
            }
        } else if (e.type == "arc_slot") {
            // Capsule outline around a circular spine (center, radius; CCW a0->a1 in start/end).
            // Outer arc (r+hw), inner arc (r-hw), and a half-circle cap at each spine endpoint.
            const float D2R = 3.14159265f / 180.0f;
            float hw = e.width * 0.5f;
            float a0 = e.start, a1 = e.end; if (a1 < a0) a1 += 360.0f;
            auto arcPts = [&](float rad, int seg) {
                std::vector<ImVec2> pts; pts.reserve(seg + 1);
                for (int i = 0; i <= seg; ++i) {
                    float a = (a0 + (a1 - a0) * (i / (float)seg)) * D2R;
                    pts.push_back(uvToScreen(e.center[0] + rad * std::cos(a),
                                             e.center[1] + rad * std::sin(a)));
                }
                return pts;
            };
            auto capPts = [&](float aDeg) {
                float sa = aDeg * D2R;
                float sx = e.center[0] + e.radius * std::cos(sa);
                float sy = e.center[1] + e.radius * std::sin(sa);
                int seg = 24;
                std::vector<ImVec2> pts; pts.reserve(seg + 1);
                for (int i = 0; i <= seg; ++i) {
                    float t = (aDeg - 90.0f + 180.0f * (i / (float)seg)) * D2R;
                    pts.push_back(uvToScreen(sx + hw * std::cos(t), sy + hw * std::sin(t)));
                }
                return pts;
            };
            strokePoly(arcPts(e.radius + hw, 64));
            strokePoly(arcPts(e.radius - hw, 64));
            strokePoly(capPts(e.start));
            strokePoly(capPts(e.end));
        } else if (e.type == "point") {
            // C42: dot + small cross so a lone point reads as geometry, not a stray handle.
            ImVec2 p = uvToScreen(e.p1[0], e.p1[1]);
            dl->AddCircleFilled(p, 3.0f, cc);
            dl->AddLine(ImVec2(p.x - 6, p.y), ImVec2(p.x + 6, p.y), cc, 1.2f);
            dl->AddLine(ImVec2(p.x, p.y - 6), ImVec2(p.x, p.y + 6), cc, 1.2f);
        } else if (e.type == "ellipse") {
            // C42: parametric sample with the rotation applied (rot stored in degrees).
            const float D2R = 3.14159265f / 180.0f;
            float ca = std::cos(e.rot * D2R), sa = std::sin(e.rot * D2R);
            int seg = 64;
            std::vector<ImVec2> pts; pts.reserve(seg + 1);
            for (int i = 0; i <= seg; ++i) {
                float a = i / (float)seg * 6.2831853f;
                float lx = e.rx * std::cos(a), ly = e.ry * std::sin(a);
                pts.push_back(uvToScreen(e.center[0] + lx * ca - ly * sa,
                                         e.center[1] + lx * sa + ly * ca));
            }
            strokePoly(pts);
            dl->AddCircleFilled(uvToScreen(e.center[0], e.center[1]), 2.5f, cc);
        } else if (e.type == "spline" || e.type == "style_spline") {
            // C42/M-1: an interpolating "spline" draws the SMOOTH curve through its points
            // (Catmull-Rom), so the viewport matches the geometry the engine sweeps/revolves.
            // A "style_spline" is a by-poles curve — keep showing its control polygon (the
            // SolidWorks convention) plus its pole markers.
            const std::vector<ImVec2>& src =
                (e.type == "spline") ? catmullRomUV(e.pts) : e.pts;
            std::vector<ImVec2> pts; pts.reserve(src.size());
            for (const auto& q : src) pts.push_back(uvToScreen(q.x, q.y));
            strokePoly(pts);
            for (const auto& q : e.pts) dl->AddCircleFilled(uvToScreen(q.x, q.y), 2.5f, cc);
        }
    }

    // C50: selected POINT handles (vertices/centers picked for relations) — filled accent squares.
    // Entity-level selection already thickens the whole entity above; this marks sub-handle picks.
    for (const auto& selh : selIds_) {
        auto dot = selh.find('.');
        if (dot == std::string::npos) continue;   // bare entity id, handled in the entity loop
        const SketchEntity* e = entById(selh.substr(0, dot));
        if (!e) continue;
        std::string sub = selh.substr(dot + 1);
        ImVec2 p;
        if (sub == "p1")          p = uvToScreen(e->p1[0], e->p1[1]);
        else if (sub == "p2")     p = uvToScreen(e->p2[0], e->p2[1]);
        else if (sub == "center") p = uvToScreen(e->center[0], e->center[1]);
        else if (sub == "p")      p = uvToScreen(e->p1[0], e->p1[1]);
        else if (sub == "start_angle") {
            const float D2R = 3.14159265f / 180.0f;
            p = uvToScreen(e->center[0] + e->radius * std::cos(e->start * D2R),
                           e->center[1] + e->radius * std::sin(e->start * D2R));
        }
        else if (sub == "end_angle") {
            const float D2R = 3.14159265f / 180.0f;
            p = uvToScreen(e->center[0] + e->radius * std::cos(e->end * D2R),
                           e->center[1] + e->radius * std::sin(e->end * D2R));
        }
        else if (sub == "rim") { continue; }   // floating cursor-dependent handle, not persisted
        else continue;
        dl->AddRectFilled(ImVec2(p.x - 4, p.y - 4), ImVec2(p.x + 4, p.y + 4), col(pal.accent), 1.5f);
    }

    // ---- C37-D: dimension annotations (only rendered when SmartDim tool active, T-BL41) ----
    // T-BL41: dimensions are hidden by default; they only render when SmartDim is active.
    // dimHoverCid_ must be cleared when hidden so drag/delete handlers don't fire on stale ids.
    if (tool_ != SketchTool::SmartDim) { dimHoverCid_.clear(); }
    if (tool_ == SketchTool::SmartDim) {
        const ImU32 dimLineCol = col(pal.sketchDimLine, 0.85f);
        const ImU32 dimTextCol = col(pal.sketchInk);
        const ImU32 dimDrivenCol = col(pal.consoleDim);     // dimmer for driven (reference) dims
        const ImU32 dimHoverCol = col(pal.accent);
        const ImU32 dimErrCol = col(pal.statusErr);         // C50f: conflicting dims/badges in red
        // C50f: is this constraint id one the solver flagged as conflicting? Conflicting dims/badges
        // render in the error color (SolidWorks paints fighting relations red), matching the entity
        // coloring already done by statusCol.
        auto isConflict = [&](const std::string& cid) {
            // Redundant driving dims count too: a consistent-but-redundant dim still over-defines
            // (SolidWorks paints it red the moment you keep it driving), and the redundant tag is
            // the only hint which dim of the dependent set to delete or make driven.
            return std::find(conflicting_.begin(), conflicting_.end(), cid) != conflicting_.end()
                || std::find(redundant_.begin(),  redundant_.end(),  cid) != redundant_.end();
        };
        // Dimension annotations. C50g: SolidWorks-style picking — a dim is hovered when the
        // mouse is inside its TEXT box (padded ~4px*uiScale) or within ~6px*uiScale of its
        // DIMENSION LINE (not the extension lines). We draw every dim non-hovered first while
        // collecting per-dim hit geometry, resolve the hover (text beats line, then nearest),
        // then re-draw just the winner on top in the hover color. Drawing the geometry is
        // independent of the hover flag (only thickness/color differ), so the overlap is exact
        // and the placement math lives in one place (drawDimension) — no drift.
        struct DimDrawn { const SketchConstraint* c; float pu, pv; bool conflict; DimHit hit; };
        std::vector<DimDrawn> drawn;
        drawn.reserve(cons_.size());
        for (const auto& c : cons_) {
            if (!c.isDimensional()) continue;
            // placement: stored -> autoPlace_ cache -> default (persist into the cache lazily).
            float pu, pv;
            bool dragging = dimDragging_ && dimDragCid_ == c.id;
            if (dragging) { pu = dimDragPlace_.x; pv = dimDragPlace_.y; }
            else if (c.hasPlacement) { pu = c.placement[0]; pv = c.placement[1]; }
            else {
                auto it = autoPlace_.find(c.id);
                if (it != autoPlace_.end()) { pu = it->second.x; pv = it->second.y; }
                else if (defaultPlacement(c, pu, pv)) autoPlace_[c.id] = ImVec2(pu, pv);
                else continue;
            }
            bool conflict = isConflict(c.id);
            ImU32 lc = conflict ? dimErrCol : dimLineCol;
            ImU32 tc = conflict ? dimErrCol : (c.driven ? dimDrivenCol : dimTextCol);
            DimHit hit; hit.cid = c.id;
            drawDimension(dl, c, lc, tc, false, pu, pv, &hit);
            drawn.push_back({&c, pu, pv, conflict, hit});
        }
        // Resolve hover from the collected geometry. Prefer a TEXT-box hit over a line hit; among
        // equals, nearest wins (text: distance to box center; line: perpendicular distance).
        std::string newHover;
        if (imgHovered) {
            const ImVec2 m = io.MousePos;
            const float lineThresh = 6.0f * ui::sketch::uiScale();
            const float lineThresh2 = lineThresh * lineThresh;
            auto segDist2 = [](const ImVec2& p, const ImVec2& a, const ImVec2& b) {
                float vx = b.x - a.x, vy = b.y - a.y; float L2 = vx * vx + vy * vy;
                float t = L2 > 1e-6f ? ((p.x - a.x) * vx + (p.y - a.y) * vy) / L2 : 0.0f;
                if (t < 0) t = 0; else if (t > 1) t = 1;
                float cx = a.x + vx * t, cy = a.y + vy * t;
                return (p.x - cx) * (p.x - cx) + (p.y - cy) * (p.y - cy);
            };
            bool haveText = false; float bestText2 = 0, bestLine2 = 0;
            for (const auto& d : drawn) {
                const DimHit& h = d.hit;
                bool inText = m.x >= h.textMin.x && m.x <= h.textMax.x &&
                              m.y >= h.textMin.y && m.y <= h.textMax.y;
                if (inText) {
                    float cx = (h.textMin.x + h.textMax.x) * 0.5f, cy = (h.textMin.y + h.textMax.y) * 0.5f;
                    float d2 = (m.x - cx) * (m.x - cx) + (m.y - cy) * (m.y - cy);
                    if (!haveText || d2 < bestText2) { haveText = true; bestText2 = d2; newHover = h.cid; }
                    continue;
                }
                if (haveText) continue;   // a text hit already beats any line hit
                float ld2 = lineThresh2;
                for (int s = 0; s < h.nSeg; ++s)
                    ld2 = std::min(ld2, segDist2(m, h.seg[s][0], h.seg[s][1]));
                if (ld2 < lineThresh2 && (newHover.empty() || ld2 < bestLine2)) {
                    bestLine2 = ld2; newHover = h.cid;
                }
            }
        }
        // Re-draw the hovered dim on top in the hover color (exact overlap with its first pass).
        if (!newHover.empty()) {
            for (const auto& d : drawn) {
                if (d.c->id != newHover) continue;
                drawDimension(dl, *d.c, dimHoverCol, dimHoverCol, true, d.pu, d.pv);
                break;
            }
        }
        dimHoverCid_ = newHover;
    }

    // ---- selection / drag handles + interaction ----
    float mu = 0, mv = 0;
    if (imgHovered) screenToUV(io.MousePos, mu, mv);

    // ---- C37-D: dimension text drag + double-click-to-edit + Delete ----
    // C47: also active while Smart Dimension is armed (SolidWorks lets you drag/edit existing
    // dims mid-dimensioning); the SmartDim pick-click below skips text-hovered clicks.
    if ((tool_ == SketchTool::None || tool_ == SketchTool::SmartDim) && !dimPopupOpen_) {
        // Begin a drag on the hovered dim text. C38 (S4): the second click of a double-click also
        // fires IsMouseClicked; exclude it so a double-click-to-edit never spawns a phantom drag
        // (which would commit a stray sketch_set_dim placement on release).
        if (imgHovered && !dimDragging_ && !dragging_ && !dimHoverCid_.empty() &&
            ImGui::IsMouseClicked(ImGuiMouseButton_Left) &&
            !ImGui::IsMouseDoubleClicked(ImGuiMouseButton_Left)) {
            dimDragging_ = true; dimDragCid_ = dimHoverCid_; dimDragPlace_ = ImVec2(mu, mv);
        }
        if (dimDragging_) {
            dimDragPlace_ = ImVec2(mu, mv);
            if (ImGui::IsMouseReleased(ImGuiMouseButton_Left)) {
                // Persist the new placement via sketch_set_dim {placement}.
                nlohmann::json op = {{"op", "sketch_set_dim"}, {"sketch", fid_}, {"id", dimDragCid_},
                    {"placement", nlohmann::json::array({dimDragPlace_.x, dimDragPlace_.y})}};
                auto r = st.docMirror().applyOps(st.worker(), nlohmann::json::array({op}));
                if (!r.ok) st.toast(ui::sketch::ToastLevel::Warn, "Move dimension: " + r.error_message);
                else { autoPlace_[dimDragCid_] = dimDragPlace_;
                       if (!r.aicm_path.empty()) { std::string le; st.loadDocMesh(r.aicm_path, le); }
                       resolve(st); }
                dimDragging_ = false; dimDragCid_.clear();
            }
        }
        // Double-click a dim text -> reopen the Modify box prefilled.
        if (imgHovered && !dimHoverCid_.empty() && ImGui::IsMouseDoubleClicked(ImGuiMouseButton_Left)) {
            // C38 (S4): if a drag somehow began on the first click of this double-click, cancel it so
            // its release-handler can't also fire a placement op behind the edit box.
            dimDragging_ = false; dimDragCid_.clear();
            for (const auto& c : cons_) {
                if (c.id != dimHoverCid_) continue;
                dimEditCid_ = c.id; dimType_ = c.type; dimIsAngle_ = (c.type == "angle");
                dimMeasured_ = c.hasMeasured ? c.measured : (c.hasValue ? c.value : 0.0);
                dimPopupDriven_ = c.driven;
                if (c.hasExpr) std::snprintf(dimBuf_.data(), dimBuf_.size(), "=%s", c.expr.c_str());
                else std::snprintf(dimBuf_.data(), dimBuf_.size(), "%s", fmtLen(dimMeasured_).c_str());
                dimError_.clear(); dimPopupOpen_ = true; dimPopupFocus_ = true; dimPopupPos_ = io.MousePos;
                break;
            }
        }
        // C41 (T2): Delete/Backspace removes the selected entities (one ops array for all of
        // them, then re-solve). A hovered dimension text takes priority (handler below) — keep
        // this gated on dimHoverCid_.empty() and BEFORE that handler (it clears the hover id).
        if ((!selIds_.empty() || !selRelIds_.empty()) && dimHoverCid_.empty() && !io.WantTextInput &&
            (ImGui::IsKeyPressed(ImGuiKey_Delete) || ImGui::IsKeyPressed(ImGuiKey_Backspace))) {
            // C50: selection may hold point handles ("e3.p2") — delete the OWNING entity, deduped.
            // C51: selected relations are deleted in the same batch via sketch_delete_constraint.
            nlohmann::json ops = nlohmann::json::array();
            std::vector<std::string> done;
            for (const auto& sel : selIds_) {
                std::string base = sel.substr(0, sel.find('.'));
                if (base == "origin") continue;   // the virtual origin can't be deleted
                if (std::find(done.begin(), done.end(), base) != done.end()) continue;
                done.push_back(base);
                ops.push_back({{"op", "sketch_delete_entity"}, {"sketch", fid_}, {"id", base}});
            }
            for (const auto& cid : selRelIds_)
                ops.push_back({{"op", "sketch_delete_constraint"}, {"sketch", fid_}, {"id", cid}});
            auto r = st.docMirror().applyOps(st.worker(), ops);
            if (!r.ok) st.toast(ui::sketch::ToastLevel::Error, "Delete: " + r.error_message);
            else {
                if (!r.aicm_path.empty()) { std::string le; st.loadDocMesh(r.aicm_path, le); }
                resolve(st);
            }
            selIds_.clear(); selRelIds_.clear();
        }
        // Delete key removes the hovered dim.
        if (!dimHoverCid_.empty() && ImGui::IsKeyPressed(ImGuiKey_Delete)) {
            nlohmann::json op = {{"op", "sketch_delete_constraint"}, {"sketch", fid_}, {"id", dimHoverCid_}};
            auto r = st.docMirror().applyOps(st.worker(), nlohmann::json::array({op}));
            if (!r.ok) st.toast(ui::sketch::ToastLevel::Error, "Delete dimension: " + r.error_message);
            else { if (!r.aicm_path.empty()) { std::string le; st.loadDocMesh(r.aicm_path, le); }
                   autoPlace_.erase(dimHoverCid_); resolve(st); }
            dimHoverCid_.clear();
        }
    }

    // Drag / relation-badge interaction: active when no drawing tool is armed OR when SmartDim is
    // active. SmartDim needs badge hover + click (to select/delete a relation) and right-click
    // context menu (to add a new relation via "Add Relation..."). T-BL41: extended to SmartDim.
    if (tool_ == SketchTool::None || tool_ == SketchTool::SmartDim) {
        // C37-D: a hovered/dragging dimension text claims the click (dim drag/edit above handles it).
        auto toggleSel = [&](const std::string& id) {
            auto it = std::find(selIds_.begin(), selIds_.end(), id);
            if (io.KeyCtrl) { if (it != selIds_.end()) selIds_.erase(it); else selIds_.push_back(id); }
            else { selIds_.clear(); selRelIds_.clear(); selIds_.push_back(id); }   // C51: drop relations too
        };
        // C51: also toggle a geometric RELATION into the relation selection (separate from the
        // entity selIds_ buffer). A non-Ctrl pick replaces both selections.
        auto toggleRel = [&](const std::string& cid) {
            auto it = std::find(selRelIds_.begin(), selRelIds_.end(), cid);
            if (io.KeyCtrl) { if (it != selRelIds_.end()) selRelIds_.erase(it); else selRelIds_.push_back(cid); }
            else { selIds_.clear(); selRelIds_.clear(); selRelIds_.push_back(cid); }
        };

        // ---- C51: hover target (what a click will grab). Priority handle > relation badge >
        // entity (or its dwelled loop) > region interior. Computed once and reused for the
        // pre-highlight AND the press, so the cursor's affordance and its action always agree. ----
        bool idleHover = imgHovered && !dragging_ && !boxSelecting_ && !groupDragging_ &&
                         !dragArmed_ && !groupDragArmed_ && !dimDragging_ && dimHoverCid_.empty();
        std::string hovHandle; float hovHu = 0, hovHv = 0;
        std::string hovRel, hovEnt;
        int hovRegion = -1;
        if (idleHover) {
            if (hitTestHandle(io.MousePos, hovHandle, hovHu, hovHv) > 0) {
                // point handle wins
            } else {
                int rb = hitTestRelationBadge(io.MousePos);
                if (rb >= 0) hovRel = relHits_[rb].cid;
                else {
                    int ei = hitTestEntity(io.MousePos);
                    if (ei >= 0) hovEnt = ents_[ei].id;
                    else { int r = hitTestRegion(io.MousePos); if (regionValid(r)) hovRegion = r; }
                }
            }
        }
        hoverRelCid_ = hovRel;   // feeds the badge tint on the next frame's render pass

        // C51: entity-edge dwell -> loop preview. Resting on the same loop-member edge for kLoopDwell
        // grows the pick from that lone edge to its whole closed loop (SolidWorks-ish loop pick).
        // Clicking the INSIDE of a profile selects the loop with no delay (handled via hovRegion).
        constexpr float kLoopDwell = 0.7f;   // ~"a second" of rest to expand an edge to its loop
        bool slow = std::hypot(io.MouseDelta.x, io.MouseDelta.y) < 2.5f;
        int loopOfHov = hovEnt.empty() ? -1 : regionContaining(hovEnt);
        if (!hovEnt.empty() && hovEnt == dwellEnt_ && slow && loopOfHov >= 0) dwellAccum_ += io.DeltaTime;
        else { dwellEnt_ = hovEnt; dwellAccum_ = 0.0f; }
        entityLoopRegion_ = (loopOfHov >= 0 && dwellAccum_ >= kLoopDwell) ? loopOfHov : -1;

        // The loop a click would grab: a hovered interior immediately, else a dwelled edge's loop.
        int effRegion = hovRegion >= 0 ? hovRegion : entityLoopRegion_;

        // ---- C51: pre-highlight — outline exactly what a click will select, drawn on top. ----
        // T-BL41: geometry (handle/entity/region) highlights only apply in None mode; SmartDim
        // has its own cursor (Crosshair) and entity highlight logic in the SmartDim block below.
        // Badge hover highlight applies in both modes (badge tint driven by hoverRelCid_).
        const ImU32 hiCol = col(pal.accent);
        if (tool_ == SketchTool::None) {
            if (!hovHandle.empty()) {
                dl->AddCircle(uvToScreen(hovHu, hovHv), 6.0f, col(pal.hoverHighlight), 16, 2.0f);
                st.requestCursor(ui::sketch::CursorKind::Hand);
            } else if (effRegion >= 0) {
                for (const auto& id : regions_[effRegion].ids)
                    if (const SketchEntity* e = entById(id)) strokeEntityOutline(dl, *e, hiCol, 2.6f);
                hotRegion_ = effRegion;          // brighten the region fill (next frame)
                st.requestCursor(ui::sketch::CursorKind::Hand);
            } else if (!hovEnt.empty()) {
                if (const SketchEntity* e = entById(hovEnt)) strokeEntityOutline(dl, *e, hiCol, 2.6f);
                hotRegion_ = -1;
                st.requestCursor(ui::sketch::CursorKind::Hand);
            } else {
                if (hovRel.empty()) hotRegion_ = -1;   // badge hover is tinted in the render pass
                else st.requestCursor(ui::sketch::CursorKind::Hand);
            }
        } else {
            // SmartDim: badge hover cursor only (geometry cursor set by SmartDim block below).
            if (!hovRel.empty()) st.requestCursor(ui::sketch::CursorKind::Hand);
        }

        if (imgHovered && ImGui::IsMouseClicked(ImGuiMouseButton_Left) && !dragging_ &&
            !dragArmed_ && !groupDragArmed_ && !dimDragging_ && dimHoverCid_.empty()) {
            if (!hovRel.empty()) {
                toggleRel(hovRel);                    // relations select on click (no drag) — both modes
            } else if (tool_ == SketchTool::None) {
                // Geometry pick/drag actions only in selection mode (SmartDim has its own click path).
                if (!hovHandle.empty()) {
                    // C50: ARM a potential drag on this point handle — release without moving selects
                    // the handle; a move past threshold reshapes (single point).
                    dragArmed_ = true; pendingHandle_ = hovHandle; dragDownPos_ = io.MousePos;
                } else if (effRegion >= 0) {
                    // C51: profile interior, or a dwelled edge's loop -> arm a group drag of the loop;
                    // release without moving selects the whole loop.
                    groupDragArmed_ = true; boxStart_ = io.MousePos; boxCtrl_ = io.KeyCtrl;
                    groupClickKind_ = 2; groupClickRegion_ = effRegion; groupClickEnt_.clear();
                } else if (!hovEnt.empty()) {
                    // C51: entity body -> arm a group drag (the whole selection if this entity is in
                    // it, else just this entity); release without moving selects it.
                    groupDragArmed_ = true; boxStart_ = io.MousePos; boxCtrl_ = io.KeyCtrl;
                    groupClickKind_ = 1; groupClickEnt_ = hovEnt; groupClickRegion_ = -1;
                } else {
                    boxArmed_ = true; boxStart_ = io.MousePos; boxCtrl_ = io.KeyCtrl;   // empty canvas
                }
            }
        }
        // Armed handle: promote to a real (single-point reshape) drag past the threshold. The
        // origin is fixed at (0,0) — it stays armed (never promotes), so releasing simply selects it.
        if (dragArmed_ && !dragging_ && pendingHandle_ != "origin" &&
            ImGui::IsMouseDown(ImGuiMouseButton_Left)) {
            if (std::hypot(io.MousePos.x - dragDownPos_.x, io.MousePos.y - dragDownPos_.y) > 4.0f) {
                dragging_ = true; dragHandle_ = pendingHandle_; dragFrame_ = -1; dragArmed_ = false;
            }
        }
        // C51: armed group -> promote to a live profile/selection translate past the threshold.
        if (groupDragArmed_ && !groupDragging_ && ImGui::IsMouseDown(ImGuiMouseButton_Left)) {
            if (std::hypot(io.MousePos.x - boxStart_.x, io.MousePos.y - boxStart_.y) > 4.0f) {
                std::vector<std::string> set;
                if (groupClickKind_ == 2 && groupClickRegion_ >= 0 &&
                    groupClickRegion_ < (int)regions_.size()) {
                    set = regions_[groupClickRegion_].ids;
                } else if (!groupClickEnt_.empty() && isSelected(groupClickEnt_)) {
                    for (const auto& s : selIds_) set.push_back(s.substr(0, s.find('.')));
                } else if (!groupClickEnt_.empty()) {
                    set.push_back(groupClickEnt_);
                }
                float su, sv; screenToUV(boxStart_, su, sv);
                beginGroupDrag(set, su, sv);
                groupDragArmed_ = false; dragFrame_ = -1;
            }
        }
        // C51: armed box -> promote to a live rubber band past the threshold.
        if (boxArmed_ && !boxSelecting_ && ImGui::IsMouseDown(ImGuiMouseButton_Left)) {
            if (std::hypot(io.MousePos.x - boxStart_.x, io.MousePos.y - boxStart_.y) > 4.0f)
                boxSelecting_ = true;
        }
        // C51: feed the live group translate (throttled once/frame, like the point drag). Compute
        // uv straight from the cursor so the drag tracks even when it strays past the image edge.
        if (groupDragging_ && ImGui::IsMouseDown(ImGuiMouseButton_Left)) {
            int frame = ImGui::GetFrameCount();
            if (frame != dragFrame_) {
                dragFrame_ = frame;
                float gu, gv; screenToUV(io.MousePos, gu, gv);
                updateGroupDrag(st, gu, gv);
            }
            st.requestCursor(ui::sketch::CursorKind::Hand);
        }
        if (dragging_ && ImGui::IsMouseDown(ImGuiMouseButton_Left)) {
            int frame = ImGui::GetFrameCount();
            if (frame != dragFrame_ && imgHovered) {
                dragFrame_ = frame;
                nlohmann::json res;
                // Most handles target the cursor model point (u,v). The new scalar handles
                // (.rim / .start_angle / .end_angle) instead target a single solver variable, so the
                // scalar goes in xy[0]; xy[1] is ignored by the engine's 1-variable soft-target drag.
                nlohmann::json extra;
                auto hSuf = [](const std::string& h, const char* suf) {
                    auto p = h.rfind('.');
                    return p != std::string::npos && h.substr(p + 1) == suf;
                };
                std::string hEid = dragHandle_.substr(0, dragHandle_.find('.'));
                const SketchEntity* hEnt = entById(hEid);
                if (hSuf(dragHandle_, "rim") && hEnt) {
                    double dr = std::hypot((double)mu - hEnt->center[0],
                                           (double)mv - hEnt->center[1]);
                    extra = {{"handle", dragHandle_},
                             {"xy", nlohmann::json::array({dr, 0.0})}};
                } else if ((hSuf(dragHandle_, "start_angle") ||
                            hSuf(dragHandle_, "end_angle")) && hEnt) {
                    double ang = std::atan2((double)mv - hEnt->center[1],
                                            (double)mu - hEnt->center[0])
                                 * 180.0 / 3.14159265358979;
                    if (ang < 0) ang += 360.0;
                    extra = {{"handle", dragHandle_},
                             {"xy", nlohmann::json::array({ang, 0.0})}};
                } else {
                    extra = {{"handle", dragHandle_},
                             {"xy", nlohmann::json::array({(double)mu, (double)mv})}};
                }
                if (callSketch(st, "sketch_drag", extra, res)) applyEnterResult(res);
            }
        }
        if (ImGui::IsMouseReleased(ImGuiMouseButton_Left)) {
            if (dragging_) { dragging_ = false; commitDrag(st); }
            else if (groupDragging_) {
                // C51: profile/selection translate finished -> persist it as one undoable batch.
                groupDragging_ = false; commitDrag(st);
            }
            else if (dragArmed_) {
                // Released without dragging -> select the point handle (vertex/center pick).
                // The drag-only rim/arc-angle handles (.rim/.start_angle/.end_angle) are not real
                // sub-entities: a click without a drag should select the OWNING entity instead, so
                // toggle-construction / relations get a resolvable id (not "c1.rim").
                std::string selH = pendingHandle_;
                auto dot = selH.rfind('.');
                if (dot != std::string::npos) {
                    std::string suf = selH.substr(dot);
                    if (suf == ".rim" || suf == ".start_angle" || suf == ".end_angle")
                        selH = selH.substr(0, dot);
                }
                toggleSel(selH);
            }
            else if (groupDragArmed_) {
                // C51: press without a drag -> plain click. Select the pressed entity, or the
                // whole loop if the press was inside a hot profile.
                if (groupClickKind_ == 2 && groupClickRegion_ >= 0 &&
                    groupClickRegion_ < (int)regions_.size()) {
                    if (!boxCtrl_) { selIds_.clear(); selRelIds_.clear(); }
                    for (const auto& id : regions_[groupClickRegion_].ids)
                        if (!isSelected(id)) selIds_.push_back(id);
                } else if (!groupClickEnt_.empty()) {
                    toggleSel(groupClickEnt_);
                }
            }
            // C51: finish a box drag, or a plain empty-canvas click.
            else if (boxSelecting_) {
                bool crossing = io.MousePos.x < boxStart_.x;   // right->left = crossing
                boxSelect(boxStart_, io.MousePos, crossing, boxCtrl_);
            } else if (boxArmed_ && !boxCtrl_) {
                selIds_.clear(); selRelIds_.clear();   // C41 (T2): empty-canvas click clears selection
            }
            dragArmed_ = false;
            boxArmed_ = boxSelecting_ = false;
            groupDragArmed_ = false; groupClickKind_ = 0; groupClickEnt_.clear(); groupClickRegion_ = -1;
        }
        // C51: live rubber band. Solid accent = window (L->R, fully-enclosed); dashed amber =
        // crossing (R->L, touch), mirroring SolidWorks' two box-select modes.
        if (boxSelecting_) {
            bool crossing = io.MousePos.x < boxStart_.x;
            ImVec2 lo(std::fmin(boxStart_.x, io.MousePos.x), std::fmin(boxStart_.y, io.MousePos.y));
            ImVec2 hi(std::fmax(boxStart_.x, io.MousePos.x), std::fmax(boxStart_.y, io.MousePos.y));
            ImU32 edge = crossing ? col(pal.sketchInfer) : col(pal.accent, 0.9f);
            dl->AddRectFilled(lo, hi, crossing ? col(pal.sketchInfer, 0.10f) : col(pal.accent, 0.10f));
            if (crossing) {
                drawDashedLine(dl, lo, ImVec2(hi.x, lo.y), edge, 1.4f, 6.0f, 4.0f);
                drawDashedLine(dl, ImVec2(hi.x, lo.y), hi, edge, 1.4f, 6.0f, 4.0f);
                drawDashedLine(dl, hi, ImVec2(lo.x, hi.y), edge, 1.4f, 6.0f, 4.0f);
                drawDashedLine(dl, ImVec2(lo.x, hi.y), lo, edge, 1.4f, 6.0f, 4.0f);
            } else {
                dl->AddRect(lo, hi, edge, 0.0f, 0, 1.4f);
            }
        }
        // (Handle/entity/loop/relation hover highlight is drawn by the C51 pre-highlight above.)

        // C37-D: right-click -> small context menu (gestures are off in-sketch, so the RMB is free).
        // T-BL41: also active in SmartDim when no picks are in progress (SmartDim RMB with picks
        // clears the pick buffer in the SmartDim block below — that takes priority).
        bool rmbForMenu = imgHovered && ImGui::IsMouseClicked(ImGuiMouseButton_Right) && !dimPopupOpen_
                          && (tool_ == SketchTool::None || dimPicks_.empty());
        if (rmbForMenu) ImGui::OpenPopup("##SketchCtx");
    }
    if (ImGui::BeginPopup("##SketchCtx")) {
        if (!selIds_.empty()) {
            if (ImGui::MenuItem("Add Relation...")) {
                relIds_ = selIds_; relPopupOpen_ = true; relPopupPos_ = ImGui::GetMousePos();
            }
            ImGui::Separator();
        }
        if (ImGui::MenuItem("Select")) resetTool();
        if (ImGui::MenuItem("Smart Dimension")) { resetTool(); tool_ = SketchTool::SmartDim; }
        if (ImGui::MenuItem("Grid", nullptr, showGrid_)) showGrid_ = !showGrid_;
        if (ImGui::MenuItem("Automatic Relations", nullptr, autoAlignRelations_)) {
            autoAlignRelations_ = !autoAlignRelations_;
            st.toast(ui::sketch::ToastLevel::Info, autoAlignRelations_
                     ? "Automatic relations: On (snapped alignments add vertical/horizontal relations)"
                     : "Automatic relations: Off (alignment guides stay positional)");
        }
        ImGui::Separator();
        if (ImGui::MenuItem("Exit Sketch")) { ImGui::CloseCurrentPopup(); ImGui::EndPopup();
                                              exitSketch(st, /*keep=*/true); dl->PopClipRect(); return; }
        ImGui::EndPopup();
    }

    // ---- C29-B sketch-editing tools (Trim / Extend / Offset) ----
    // These have bespoke pick semantics (whole-entity hover + ops), so they run BEFORE the generic
    // drawing-tool block and short-circuit it. Each refreshes the entity set via the ops response
    // path (loadDocMesh + resolve), exactly like sketch_add_entity; op errors are logged and the
    // tool stays active. Esc exits.
    bool editTool = (tool_ == SketchTool::Trim || tool_ == SketchTool::Extend ||
                     tool_ == SketchTool::Offset ||
                     tool_ == SketchTool::SkFillet || tool_ == SketchTool::SkChamfer ||
                     tool_ == SketchTool::Mirror || tool_ == SketchTool::LinPattern ||
                     tool_ == SketchTool::CircPattern || tool_ == SketchTool::Move);
    if (editTool) {
        int hovered = imgHovered ? hitTestEntity(io.MousePos) : -1;

        // ----- TRIM (power trim): highlight the exact doomed SPAN in red; click trims it; -----
        // ----- holding LMB and sweeping across entities trims each one crossed (SW style). -----
        if (tool_ == SketchTool::Trim) {
            const ImU32 rc = col(pal.statusErr, 0.95f);
            // Preview: the span a click would remove (client-side mirror of the engine split).
            // Spans of unsupported types (slot etc.) draw nothing — the click logs the error.
            if (hovered >= 0) {
                std::vector<ImVec2> span; bool whole = false;
                if (trimDoomedSpan(hovered, mu, mv, span, whole)) {
                    if (whole) {
                        // No interior crossing: the whole entity goes (engine deletes it).
                        const SketchEntity& e = ents_[hovered];
                        if (e.type == "line") {
                            dl->AddLine(uvToScreen(e.p1[0], e.p1[1]),
                                        uvToScreen(e.p2[0], e.p2[1]), rc, 3.5f);
                        } else {
                            float a0 = 0, a1 = 6.2831853f;
                            if (e.type == "arc") {
                                a0 = e.start * 3.14159265f / 180.0f; a1 = e.end * 3.14159265f / 180.0f;
                                if (!e.ccw) { float t = a0; a0 = a1; a1 = t; }
                                if (a1 < a0) a1 += 6.2831853f;
                            }
                            ImVec2 prev; int seg = 64;
                            for (int i = 0; i <= seg; ++i) {
                                float a = a0 + (a1 - a0) * (i / (float)seg);
                                ImVec2 p = uvToScreen(e.center[0] + e.radius * std::cos(a),
                                                      e.center[1] + e.radius * std::sin(a));
                                if (i) dl->AddLine(prev, p, rc, 3.5f); prev = p;
                            }
                        }
                    } else {
                        for (size_t i = 1; i < span.size(); ++i)
                            dl->AddLine(uvToScreen(span[i - 1].x, span[i - 1].y),
                                        uvToScreen(span[i].x, span[i].y), rc, 3.5f);
                    }
                }
            }
            // One trim op at the current cursor (shared by click + sweep).
            auto fireTrim = [&](int idx) -> bool {
                const SketchEntity& e = ents_[idx];
                if (e.type != "line" && e.type != "circle" && e.type != "arc") {
                    st.addLog("[sketch] Trim: unsupported entity type '" + e.type + "'");
                    st.toast(ui::sketch::ToastLevel::Info,
                             "Trim works on lines, circles, and arcs — not " + e.type);
                    return false;
                }
                nlohmann::json op = {{"op", "sketch_trim"}, {"sketch", fid_},
                    {"entity", e.id}, {"point", nlohmann::json::array({mu, mv})}};
                auto r = st.docMirror().applyOps(st.worker(), nlohmann::json::array({op}));
                if (!r.ok) { st.addLog("[sketch] trim failed: " + r.error_message); return false; }
                if (!r.aicm_path.empty()) { std::string le; st.loadDocMesh(r.aicm_path, le); }
                resolve(st);   // refresh entities/statuses (same path as add_entity)
                return true;
            };
            if (imgHovered && ImGui::IsMouseClicked(ImGuiMouseButton_Left) && hovered >= 0) {
                if (fireTrim(hovered)) { trimSweepLast_ = io.MousePos; trimSweeping_ = true; }
            } else if (trimSweeping_ && ImGui::IsMouseDown(ImGuiMouseButton_Left)) {
                // Power-trim sweep: each entity the cursor crosses while LMB is held gets trimmed.
                // A small pixel gate between ops avoids re-firing on the same spot while the
                // entity list refills (ids change across every trim).
                float moved = std::hypot(io.MousePos.x - trimSweepLast_.x,
                                         io.MousePos.y - trimSweepLast_.y);
                if (imgHovered && moved > 10.0f && hovered >= 0) {
                    const SketchEntity& e = ents_[hovered];
                    if (e.type == "line" || e.type == "circle" || e.type == "arc")
                        if (fireTrim(hovered)) trimSweepLast_ = io.MousePos;
                }
            }
            if (!ImGui::IsMouseDown(ImGuiMouseButton_Left)) trimSweeping_ = false;
            if (ImGui::IsKeyPressed(ImGuiKey_Escape)) cancelTool();
        }

        // ----- EXTEND: highlight the nearer end; click sends sketch_extend with start|end. -----
        else if (tool_ == SketchTool::Extend) {
            const char* whichEnd = nullptr;     // "start"|"end" for line/arc, by nearer endpoint
            ImVec2 endPx;
            if (hovered >= 0) {
                const SketchEntity& e = ents_[hovered];
                const ImU32 ac = col(pal.accent, 0.95f);
                // Faint accent over the whole entity, bright dot on the nearer pickable end.
                if (e.type == "line") {
                    ImVec2 a = uvToScreen(e.p1[0], e.p1[1]), b = uvToScreen(e.p2[0], e.p2[1]);
                    dl->AddLine(a, b, ac, 2.5f);
                    float da = std::hypot(a.x - io.MousePos.x, a.y - io.MousePos.y);
                    float db = std::hypot(b.x - io.MousePos.x, b.y - io.MousePos.y);
                    whichEnd = da < db ? "start" : "end"; endPx = da < db ? a : b;
                } else if (e.type == "arc") {
                    ImVec2 a = uvToScreen(e.center[0] + e.radius * std::cos(e.start * 3.14159265f / 180.0f),
                                          e.center[1] + e.radius * std::sin(e.start * 3.14159265f / 180.0f));
                    ImVec2 b = uvToScreen(e.center[0] + e.radius * std::cos(e.end * 3.14159265f / 180.0f),
                                          e.center[1] + e.radius * std::sin(e.end * 3.14159265f / 180.0f));
                    float da = std::hypot(a.x - io.MousePos.x, a.y - io.MousePos.y);
                    float db = std::hypot(b.x - io.MousePos.x, b.y - io.MousePos.y);
                    whichEnd = da < db ? "start" : "end"; endPx = da < db ? a : b;
                }
                if (whichEnd) dl->AddCircleFilled(endPx, 6.0f, ac);
            }
            if (imgHovered && ImGui::IsMouseClicked(ImGuiMouseButton_Left) && hovered >= 0) {
                const SketchEntity& e = ents_[hovered];
                if (!whichEnd) {
                    st.addLog("[sketch] Extend: unsupported entity type '" + e.type + "'");
                    st.toast(ui::sketch::ToastLevel::Info,
                             "Extend works on lines and arcs — not " + e.type);
                } else {
                    nlohmann::json op = {{"op", "sketch_extend"}, {"sketch", fid_},
                        {"entity", e.id}, {"end", whichEnd}};
                    auto r = st.docMirror().applyOps(st.worker(), nlohmann::json::array({op}));
                    if (!r.ok) st.addLog("[sketch] extend failed: " + r.error_message);
                    else {
                        if (!r.aicm_path.empty()) { std::string le; st.loadDocMesh(r.aicm_path, le); }
                        resolve(st);
                    }
                }
            }
            if (ImGui::IsKeyPressed(ImGuiKey_Escape)) cancelTool();
        }

        // ----- OFFSET: pick chain -> Enter/[Offset] chip -> type distance -> click side. -----
        else if (tool_ == SketchTool::Offset) {
            auto inChain = [&](const std::string& id) {
                return std::find(offsetChain_.begin(), offsetChain_.end(), id) != offsetChain_.end();
            };
            // Tint already-selected chain entities in accent.
            for (const auto& e : ents_) {
                if (!inChain(e.id)) continue;
                const ImU32 ac = col(pal.accent, 0.95f);
                if (e.type == "line" || e.type == "slot") {
                    dl->AddLine(uvToScreen(e.p1[0], e.p1[1]), uvToScreen(e.p2[0], e.p2[1]), ac, 3.5f);
                } else if (e.type == "circle" || e.type == "arc") {
                    float a0 = 0, a1 = 6.2831853f;
                    if (e.type == "arc") {
                        a0 = e.start * 3.14159265f / 180.0f; a1 = e.end * 3.14159265f / 180.0f;
                        if (!e.ccw) { float t = a0; a0 = a1; a1 = t; }
                        if (a1 < a0) a1 += 6.2831853f;
                    }
                    ImVec2 prev; int seg = 64;
                    for (int i = 0; i <= seg; ++i) {
                        float a = a0 + (a1 - a0) * (i / (float)seg);
                        ImVec2 p = uvToScreen(e.center[0] + e.radius * std::cos(a),
                                              e.center[1] + e.radius * std::sin(a));
                        if (i) dl->AddLine(prev, p, ac, 3.5f); prev = p;
                    }
                }
            }
            if (offsetStage_ == 0) {
                // Hover preview (faint) over the would-be-added entity.
                if (hovered >= 0 && !inChain(ents_[hovered].id)) {
                    const SketchEntity& e = ents_[hovered];
                    const ImU32 hc = col(pal.hoverHighlight, 0.9f);
                    if (e.type == "line" || e.type == "slot")
                        dl->AddLine(uvToScreen(e.p1[0], e.p1[1]), uvToScreen(e.p2[0], e.p2[1]), hc, 3.0f);
                    else if (e.type == "circle" || e.type == "arc")
                        dl->AddCircle(uvToScreen(e.center[0], e.center[1]),
                                      std::hypot(uvToScreen(e.center[0] + e.radius, e.center[1]).x -
                                                 uvToScreen(e.center[0], e.center[1]).x, 0.0f),
                                      hc, 48, 3.0f);
                }
                // Click toggles an entity in/out of the chain.
                if (imgHovered && ImGui::IsMouseClicked(ImGuiMouseButton_Left) && hovered >= 0) {
                    const SketchEntity& e = ents_[hovered];
                    if (e.type != "line" && e.type != "circle" && e.type != "arc") {
                        st.addLog("[sketch] Offset: unsupported entity type '" + e.type + "'");
                    } else {
                        auto it = std::find(offsetChain_.begin(), offsetChain_.end(), e.id);
                        if (it != offsetChain_.end()) offsetChain_.erase(it);
                        else offsetChain_.push_back(e.id);
                    }
                }
                // Enter advances to the distance stage when the chain is non-empty.
                if (!offsetChain_.empty() && ImGui::IsKeyPressed(ImGuiKey_Enter)) {
                    offsetStage_ = 1; offsetFocusInput_ = true; offsetOverlayPos_ = io.MousePos;
                }
            } else if (offsetStage_ == 2) {
                // Side pick: the next click's uv is the side hint.
                if (imgHovered && ImGui::IsMouseClicked(ImGuiMouseButton_Left)) {
                    nlohmann::json eids = nlohmann::json::array();
                    for (const auto& id : offsetChain_) eids.push_back(id);
                    nlohmann::json op = {{"op", "sketch_offset"}, {"sketch", fid_},
                        {"entities", eids}, {"distance", offsetDist_},
                        {"side", nlohmann::json::array({mu, mv})}};
                    auto r = st.docMirror().applyOps(st.worker(), nlohmann::json::array({op}));
                    if (!r.ok) st.addLog("[sketch] offset failed: " + r.error_message);
                    else {
                        if (!r.aicm_path.empty()) { std::string le; st.loadDocMesh(r.aicm_path, le); }
                        resolve(st);
                    }
                    // Reset for another offset (stay in the tool), keep the distance default.
                    offsetStage_ = 0; offsetChain_.clear();
                }
            }
            if (ImGui::IsKeyPressed(ImGuiKey_Escape)) {
                escHandled = true;   // C41 (T1): stage back-out keeps the tool armed
                // Esc backs out one stage; from the chain stage it exits the tool.
                if (offsetStage_ == 2) { offsetStage_ = 1; offsetFocusInput_ = true; }
                else if (offsetStage_ == 1) offsetStage_ = 0;
                else cancelTool();
            }
        }

        // ----- SKETCH FILLET / CHAMFER (C52): pick two lines; second pick builds the op. -----
        // Engine handles LINE-LINE corners only (sketch_fillet radius / sketch_chamfer distance).
        // The radius/distance numeric field lives in a small always-on overlay pinned top-left
        // (drawn after the interaction pass) and persists across corners. On the second pick the op
        // is applied; success appends the new arc/line + re-solves and we stay armed for another
        // corner. A non-line pick toasts and is ignored. Esc clears a held first pick (or exits the
        // tool if none is held).
        else if (tool_ == SketchTool::SkFillet || tool_ == SketchTool::SkChamfer) {
            const ImU32 ac = col(pal.accent, 0.95f);
            const ImU32 hc = col(pal.hoverHighlight, 0.9f);
            // Tint the already-picked first line in accent.
            const SketchEntity* first = corner1Id_.empty() ? nullptr : entById(corner1Id_);
            if (first && first->type == "line")
                dl->AddLine(uvToScreen(first->p1[0], first->p1[1]),
                            uvToScreen(first->p2[0], first->p2[1]), ac, 3.5f);
            // Hover preview (faint) over the would-be-picked line (skip the already-picked one).
            if (hovered >= 0 && ents_[hovered].type == "line" && ents_[hovered].id != corner1Id_) {
                const SketchEntity& e = ents_[hovered];
                dl->AddLine(uvToScreen(e.p1[0], e.p1[1]), uvToScreen(e.p2[0], e.p2[1]), hc, 3.0f);
            }
            if (imgHovered && ImGui::IsMouseClicked(ImGuiMouseButton_Left) && hovered >= 0) {
                const SketchEntity& e = ents_[hovered];
                if (e.type != "line") {
                    const char* what = tool_ == SketchTool::SkFillet ? "Sketch Fillet" : "Sketch Chamfer";
                    st.addLog(std::string("[sketch] ") + what + ": pick a line, not " + e.type);
                    st.toast(ui::sketch::ToastLevel::Info,
                             std::string(what) + " works on two lines — not " + e.type);
                } else if (corner1Id_.empty()) {
                    corner1Id_ = e.id;                 // first line picked; wait for the second
                } else if (e.id == corner1Id_) {
                    // Clicking the same line again deselects it (pick a different first line).
                    corner1Id_.clear();
                } else {
                    applyCornerOp(st, e.id);           // second line: build + apply, then stay armed
                }
            }
            if (ImGui::IsKeyPressed(ImGuiKey_Escape)) {
                escHandled = true;   // C41 (T1): clear a held first pick, else exit the tool
                if (!corner1Id_.empty()) corner1Id_.clear();
                else cancelTool();
            }
        }

        // ----- C53 MIRROR / LINEAR PATTERN / CIRCULAR PATTERN: collect an entity selection -----
        // Stage 0 collects line/circle/arc entities (click toggles, like the Offset chain). Enter
        // advances: Mirror -> stage 1 pick a construction line (the mirror axis); Circular -> stage 1
        // pick the center point; Linear -> stage 1 the numeric panel. The op is applied from the
        // numeric panel / line pick (drawn after the interaction pass). Esc backs out a stage.
        else if (tool_ == SketchTool::Mirror || tool_ == SketchTool::LinPattern ||
                 tool_ == SketchTool::CircPattern) {
            const ImU32 ac = col(pal.accent, 0.95f);
            const ImU32 hc = col(pal.hoverHighlight, 0.9f);
            auto inSel = [&](const std::string& id) {
                return std::find(mpSel_.begin(), mpSel_.end(), id) != mpSel_.end();
            };
            auto strokeEnt = [&](const SketchEntity& e, ImU32 c, float th) {
                if (e.type == "line")
                    dl->AddLine(uvToScreen(e.p1[0], e.p1[1]), uvToScreen(e.p2[0], e.p2[1]), c, th);
                else if (e.type == "circle" || e.type == "arc") {
                    float a0 = 0, a1 = 6.2831853f;
                    if (e.type == "arc") {
                        a0 = e.start * 3.14159265f / 180.0f; a1 = e.end * 3.14159265f / 180.0f;
                        if (!e.ccw) { float t = a0; a0 = a1; a1 = t; }
                        if (a1 < a0) a1 += 6.2831853f;
                    }
                    ImVec2 prev; int seg = 64;
                    for (int i = 0; i <= seg; ++i) {
                        float a = a0 + (a1 - a0) * (i / (float)seg);
                        ImVec2 p = uvToScreen(e.center[0] + e.radius * std::cos(a),
                                              e.center[1] + e.radius * std::sin(a));
                        if (i) dl->AddLine(prev, p, c, th); prev = p;
                    }
                }
            };
            // Tint the current selection.
            for (const auto& e : ents_) if (inSel(e.id)) strokeEnt(e, ac, 3.5f);

            if (mpStage_ == 0) {
                if (hovered >= 0 && !inSel(ents_[hovered].id)) strokeEnt(ents_[hovered], hc, 3.0f);
                if (imgHovered && ImGui::IsMouseClicked(ImGuiMouseButton_Left) && hovered >= 0) {
                    const SketchEntity& e = ents_[hovered];
                    if (e.type != "line" && e.type != "circle" && e.type != "arc") {
                        st.toast(ui::sketch::ToastLevel::Info,
                                 "Mirror/Pattern works on lines, circles, and arcs — not " + e.type);
                    } else {
                        auto it = std::find(mpSel_.begin(), mpSel_.end(), e.id);
                        if (it != mpSel_.end()) mpSel_.erase(it); else mpSel_.push_back(e.id);
                    }
                }
                if (!mpSel_.empty() && ImGui::IsKeyPressed(ImGuiKey_Enter)) mpStage_ = 1;
            } else if (tool_ == SketchTool::Mirror && mpStage_ == 1) {
                // Pick the mirror line (must be a line entity; a construction line is the norm).
                if (hovered >= 0 && ents_[hovered].type == "line" && !inSel(ents_[hovered].id))
                    strokeEnt(ents_[hovered], hc, 3.0f);
                if (imgHovered && ImGui::IsMouseClicked(ImGuiMouseButton_Left) && hovered >= 0) {
                    const SketchEntity& e = ents_[hovered];
                    if (e.type != "line")
                        st.toast(ui::sketch::ToastLevel::Info, "Pick a line as the mirror axis, not " + e.type);
                    else if (inSel(e.id))
                        st.toast(ui::sketch::ToastLevel::Info, "The mirror line can't be one of the mirrored entities");
                    else
                        applyMirrorOp(st, e.id);   // build + apply, reset selection, stay armed
                }
            } else if (tool_ == SketchTool::CircPattern && mpStage_ == 1) {
                // Pick the center point: snap to a vertex/center if near one, else the raw cursor.
                std::string snapH; float su = mu, sv = mv;
                snapPoint(su, sv, snapH);
                dl->AddCircle(uvToScreen(su, sv), 5.0f, col(pal.accent, 0.95f), 16, 2.0f);
                if (imgHovered && ImGui::IsMouseClicked(ImGuiMouseButton_Left)) {
                    patternCenterU_ = su; patternCenterV_ = sv;
                    mpStage_ = 2;   // advance to the numeric panel
                }
            }
            // Visualize the chosen circular center while in the panel stage.
            if (tool_ == SketchTool::CircPattern && mpStage_ == 2)
                dl->AddCircle(uvToScreen(patternCenterU_, patternCenterV_), 5.0f,
                              col(pal.accent, 0.95f), 16, 2.0f);

            if (ImGui::IsKeyPressed(ImGuiKey_Escape)) {
                escHandled = true;
                if (mpStage_ > 0) { mpStage_ = (tool_ == SketchTool::CircPattern) ? mpStage_ - 1 : 0; }
                else cancelTool();
            }
        }

        // ----- C54 MOVE/COPY: stage 0 collect entities (Enter advances) -> stage 1 pick a -----
        // ----- FROM point -> stage 2 live drag to a TO point; the second click applies         -----
        // ----- sketch_move {entities, dx, dy, copy} with delta = to - from. Ctrl => copy.       -----
        else if (tool_ == SketchTool::Move) {
            const ImU32 ac = col(pal.accent, 0.95f);
            const ImU32 hc = col(pal.hoverHighlight, 0.9f);
            auto inSel = [&](const std::string& id) {
                return std::find(mpSel_.begin(), mpSel_.end(), id) != mpSel_.end();
            };
            auto strokeEnt = [&](const SketchEntity& e, ImU32 c, float th) {
                if (e.type == "line")
                    dl->AddLine(uvToScreen(e.p1[0], e.p1[1]), uvToScreen(e.p2[0], e.p2[1]), c, th);
                else if (e.type == "circle" || e.type == "arc") {
                    float a0 = 0, a1 = 6.2831853f;
                    if (e.type == "arc") {
                        a0 = e.start * 3.14159265f / 180.0f; a1 = e.end * 3.14159265f / 180.0f;
                        if (!e.ccw) { float t = a0; a0 = a1; a1 = t; }
                        if (a1 < a0) a1 += 6.2831853f;
                    }
                    ImVec2 prev; int seg = 64;
                    for (int i = 0; i <= seg; ++i) {
                        float a = a0 + (a1 - a0) * (i / (float)seg);
                        ImVec2 p = uvToScreen(e.center[0] + e.radius * std::cos(a),
                                              e.center[1] + e.radius * std::sin(a));
                        if (i) dl->AddLine(prev, p, c, th); prev = p;
                    }
                }
            };
            for (const auto& e : ents_) if (inSel(e.id)) strokeEnt(e, ac, 3.5f);

            if (mpStage_ == 0) {
                if (hovered >= 0 && !inSel(ents_[hovered].id)) strokeEnt(ents_[hovered], hc, 3.0f);
                if (imgHovered && ImGui::IsMouseClicked(ImGuiMouseButton_Left) && hovered >= 0) {
                    const SketchEntity& e = ents_[hovered];
                    if (e.type != "line" && e.type != "circle" && e.type != "arc") {
                        st.toast(ui::sketch::ToastLevel::Info,
                                 "Move works on lines, circles, and arcs — not " + e.type);
                    } else {
                        auto it = std::find(mpSel_.begin(), mpSel_.end(), e.id);
                        if (it != mpSel_.end()) mpSel_.erase(it); else mpSel_.push_back(e.id);
                    }
                }
                if (!mpSel_.empty() && ImGui::IsKeyPressed(ImGuiKey_Enter)) mpStage_ = 1;
            } else if (mpStage_ == 1) {
                // Pick the FROM anchor (snap to a vertex/center if near one).
                std::string snapH; float su = mu, sv = mv;
                snapPoint(su, sv, snapH);
                dl->AddCircle(uvToScreen(su, sv), 5.0f, ac, 16, 2.0f);
                if (imgHovered && ImGui::IsMouseClicked(ImGuiMouseButton_Left)) {
                    moveFromUV_ = ImVec2(su, sv);
                    mpStage_ = 2;
                }
            } else if (mpStage_ == 2) {
                // Live drag to the TO point; second click applies the move (Ctrl => copy).
                std::string snapH; float su = mu, sv = mv;
                snapPoint(su, sv, snapH);
                bool copy = io.KeyCtrl;
                dl->AddLine(uvToScreen(moveFromUV_.x, moveFromUV_.y), uvToScreen(su, sv),
                            ac, 1.6f);
                dl->AddCircle(uvToScreen(moveFromUV_.x, moveFromUV_.y), 4.0f, ac, 16, 1.6f);
                // Ghost-preview the selection translated by the live delta.
                float pdx = su - moveFromUV_.x, pdy = sv - moveFromUV_.y;
                const ImU32 gc = col(pal.sketchUnder, 0.8f);
                for (const auto& e : ents_) {
                    if (!inSel(e.id)) continue;
                    if (e.type == "line") {
                        dl->AddLine(uvToScreen(e.p1[0] + pdx, e.p1[1] + pdy),
                                    uvToScreen(e.p2[0] + pdx, e.p2[1] + pdy), gc, 2.0f);
                    } else if (e.type == "circle" || e.type == "arc") {
                        float a0 = 0, a1 = 6.2831853f;
                        if (e.type == "arc") {
                            a0 = e.start * 3.14159265f / 180.0f; a1 = e.end * 3.14159265f / 180.0f;
                            if (!e.ccw) { float t = a0; a0 = a1; a1 = t; }
                            if (a1 < a0) a1 += 6.2831853f;
                        }
                        ImVec2 prev; int seg = 64;
                        for (int i = 0; i <= seg; ++i) {
                            float a = a0 + (a1 - a0) * (i / (float)seg);
                            ImVec2 p = uvToScreen(e.center[0] + pdx + e.radius * std::cos(a),
                                                  e.center[1] + pdy + e.radius * std::sin(a));
                            if (i) dl->AddLine(prev, p, gc, 2.0f); prev = p;
                        }
                    }
                }
                if (imgHovered && ImGui::IsMouseClicked(ImGuiMouseButton_Left)) {
                    applyMoveOp(st, su - moveFromUV_.x, sv - moveFromUV_.y, copy);
                    // applyMoveOp resets the selection + stage 0 for another move.
                }
            }

            if (ImGui::IsKeyPressed(ImGuiKey_Escape)) {
                escHandled = true;
                if (mpStage_ > 0) mpStage_ = mpStage_ - 1;   // back out from to-pick -> from-pick -> select
                else cancelTool();
            }
        }
    }

    // ---- C53 CONVERT ENTITIES: pick MODEL edges/faces in the viewport, project each into ----
    // ---- the sketch (sketch_convert). Like Smart Dim's external-ref pick, but it applies the ----
    // ---- op immediately and stays armed for more converts. Esc exits the tool. ----
    if (tool_ == SketchTool::Convert) {
        worker::PickHit extHit{};
        if (imgHovered && st.hasDocMesh && st.idPass) {
            int px = (int)(io.MousePos.x - imgMin.x), py = (int)(io.MousePos.y - imgMin.y);
            // Edge-priority pick: a model edge on a coplanar face (normal-to a sketched face) is
            // 1 px wide and otherwise loses the center pixel to the face. ~7 px grab radius (scaled).
            int grab = (int)std::lround(7.0f * ui::sketch::uiScale());
            worker::PickHit h = toWorkerPick(st.idPass->pickFinalPreferEdge(px, py, vw, vh, grab));
            if (h.valid() && (h.kind == worker::PickKind::Edge || h.kind == worker::PickKind::Face))
                extHit = h;
        }
        st.hover = extHit;   // standard 3D hover highlight on the pickable edge/face
        if (extHit.valid() && imgHovered && ImGui::IsMouseClicked(ImGuiMouseButton_Left)) {
            std::string perr;
            std::string selStr = st.docMirror().resolvePick(
                st.worker(), extHit.body_id,
                extHit.kind == worker::PickKind::Edge ? "edge" : "face", extHit.topo_id, perr);
            nlohmann::json selJs;
            if (!selStr.empty()) { try { selJs = nlohmann::json::parse(selStr); } catch (...) {} }
            if (selJs.is_object()) {
                applyConvertRef(st, nlohmann::json{
                    {"kind", extHit.kind == worker::PickKind::Edge ? "edge" : "face"},
                    {"selector", selJs}});
            } else {
                st.toast(ui::sketch::ToastLevel::Warn, "Can't convert that geometry" +
                         (perr.empty() ? std::string() : ": " + perr));
            }
        }
        if (ImGui::IsKeyPressed(ImGuiKey_Escape)) { escHandled = true; cancelTool(); }
    }

    // ---- drawing tools: rubber-band preview + click handling ----
    if (!editTool && tool_ != SketchTool::None && tool_ != SketchTool::SmartDim &&
        tool_ != SketchTool::Convert) {
        // Snap the live cursor for preview + inference glyphs (C49: point snap + midpoint/rim
        // snap + alignment inference, see snapPoint).
        float su = mu, sv = mv; std::string snapH;
        SnapInfer infer;
        if (imgHovered) snapPoint(su, sv, snapH, &infer);

        // C49: direction inference for the Line tool's SECOND point only (polygon/spline legs
        // and the 3-pt arc chord deliberately stay free). Priority: point snap > tangent > h/v.
        bool hvH = false, hvV = false;       // live h/v chip + exact-direction force
        bool tanSnap = false;                // live tangent chip
        bool tanStartUpgrade = false;        // placement click upgrades toolSnaps_[0] rim->tan
        if (inferenceOn_ && imgHovered && tool_ == SketchTool::Line && toolUV_.size() == 1) {
            const ImVec2 a = toolUV_[0];
            const float D2R = 3.14159265f / 180.0f;
            if (snapHasPrefix(snapH, "rim:")) {
                // (a) END point on a circle/arc rim: if the segment is nearly tangent there,
                // pull it to the EXACT tangent contact seen from the fixed start point.
                const SketchEntity* ce = entById(snapH.substr(4));
                if (ce && (ce->type == "circle" || ce->type == "arc") && ce->radius > 1e-5f) {
                    float cx = ce->center[0], cy = ce->center[1], r = ce->radius;
                    float dx = a.x - cx, dy = a.y - cy;
                    float d = std::hypot(dx, dy);
                    float segx = su - a.x, segy = sv - a.y;
                    float segL = std::hypot(segx, segy);
                    if (d > r + 1e-4f && segL > 1e-5f) {       // start outside; tangent exists
                        // tangent direction at the current rim point = perp(rim - center)
                        float tx = -(sv - cy), ty = su - cx;
                        float tL = std::hypot(tx, ty);
                        float cosang = std::fabs(segx * tx + segy * ty) / (segL * tL);
                        if (std::acos(std::fmin(1.0f, cosang)) < kTangentDeg * D2R) {
                            // exact contacts: center angle alpha +/- beta, cos(beta) = r/d
                            float alpha = std::atan2(dy, dx);
                            float beta = std::acos(std::fmax(-1.0f, std::fmin(1.0f, r / d)));
                            float u1 = cx + r * std::cos(alpha + beta), v1 = cy + r * std::sin(alpha + beta);
                            float u2 = cx + r * std::cos(alpha - beta), v2 = cy + r * std::sin(alpha - beta);
                            bool first = std::hypot(u1 - su, v1 - sv) <= std::hypot(u2 - su, v2 - sv);
                            su = first ? u1 : u2; sv = first ? v1 : v2;
                            snapH = "tan:" + ce->id;
                            tanSnap = true;
                        }
                    }
                }
            } else if (snapH.empty()) {
                // (b) START point sits on a rim: keep the segment tangent at that contact.
                if (!toolSnaps_.empty() && (snapHasPrefix(toolSnaps_[0], "rim:") ||
                                            snapHasPrefix(toolSnaps_[0], "tan:"))) {
                    const SketchEntity* ce = entById(toolSnaps_[0].substr(4));
                    if (ce && (ce->type == "circle" || ce->type == "arc")) {
                        float tx = -(a.y - ce->center[1]), ty = a.x - ce->center[0];
                        float tL = std::hypot(tx, ty);
                        float segx = su - a.x, segy = sv - a.y;
                        float segL = std::hypot(segx, segy);
                        if (tL > 1e-5f && segL > 1e-5f) {
                            float cosang = std::fabs(segx * tx + segy * ty) / (segL * tL);
                            if (std::acos(std::fmin(1.0f, cosang)) < kTangentDeg * D2R) {
                                float s = (segx * tx + segy * ty) / (tL * tL);  // project on tangent
                                su = a.x + tx * s; sv = a.y + ty * s;
                                tanSnap = true; tanStartUpgrade = true;
                                infer.alignU = infer.alignV = false;  // direction owns the point now
                            }
                        }
                    }
                }
                // (c) plain h/v direction snap (kInferHVDeg) — skipped when tangent fired.
                if (!tanSnap) {
                    float du = su - a.x, dv = sv - a.y;
                    float ang = std::atan2(std::fabs(dv), std::fabs(du)) * 57.2957795f;
                    if (ang < kInferHVDeg && (du != 0.0f || dv != 0.0f)) {
                        // drop a conflicting v-alignment (h/v force wins its own axis; an
                        // alignment on the OTHER axis combines — SW-style intersection).
                        if (infer.alignV && infer.srcV.y != a.y) infer.alignV = false;
                        sv = a.y; hvH = true;
                    } else if (ang > 90.0f - kInferHVDeg) {
                        if (infer.alignU && infer.srcU.x != a.x) infer.alignU = false;
                        su = a.x; hvV = true;
                    }
                }
            }
        }
        ImVec2 sCur = uvToScreen(su, sv);
        const ImU32 prevCol = col(pal.accent, 0.9f);

        // Rubber band from the last placed point.
        if (!toolUV_.empty()) {
            ImVec2 a = uvToScreen(toolUV_.back().x, toolUV_.back().y);
            // Slot: only rubber-band the spine while the length is still being set (1 click
            // placed). Once both spine endpoints exist, the width-stage capsule preview below
            // owns the drawing — otherwise this would draw a stray line to the cursor.
            if (tool_ == SketchTool::Line ||
                (tool_ == SketchTool::Slot && toolUV_.size() == 1)) {
                dl->AddLine(a, sCur, prevCol, 1.6f);
            } else if (tool_ == SketchTool::RectCorner) {
                ImVec2 p0 = a, p1 = sCur;
                ImVec2 c1 = uvToScreen(su, toolUV_.back().y);
                ImVec2 c2 = uvToScreen(toolUV_.back().x, sv);
                dl->AddLine(p0, c1, prevCol, 1.6f); dl->AddLine(c1, p1, prevCol, 1.6f);
                dl->AddLine(p1, c2, prevCol, 1.6f); dl->AddLine(c2, p0, prevCol, 1.6f);
            } else if (tool_ == SketchTool::Circle) {
                float rpx = std::hypot(sCur.x - a.x, sCur.y - a.y);
                dl->AddCircle(a, rpx, prevCol, 48, 1.6f);
            } else if (tool_ == SketchTool::ArcCenter) {
                float rpx = std::hypot(sCur.x - a.x, sCur.y - a.y);
                dl->AddCircle(a, rpx, prevCol, 48, 1.2f);
            }
        }

        // C28-B variant previews (drawn in the sketch under-defined color, matching the placed
        // entities while still in-tool). Each branch builds the live geometry from the prior clicks
        // in toolUV_ + the snapped cursor (su,sv).
        {
            const ImU32 uCol = col(pal.sketchUnder, 0.95f);
            auto pl = [&](const std::vector<ImVec2>& uv) {  // draw a (u,v) polyline
                for (size_t i = 1; i < uv.size(); ++i)
                    dl->AddLine(uvToScreen(uv[i - 1].x, uv[i - 1].y),
                                uvToScreen(uv[i].x, uv[i].y), uCol, 1.6f);
            };
            // Arc-slot capsule preview: outer/inner arcs (r+/-hw, a0..a1 deg CCW) + two end caps.
            auto drawCapsule = [&](float cu, float cv, float rad, float a0d, float a1d, float hw) {
                const float D2R = 3.14159265f / 180.0f;
                if (a1d < a0d) a1d += 360.0f;
                auto arcPoly = [&](float rr) {
                    ImVec2 prev;
                    for (int i = 0; i <= 64; ++i) {
                        float a = (a0d + (a1d - a0d) * (i / 64.0f)) * D2R;
                        ImVec2 q = uvToScreen(cu + rr * std::cos(a), cv + rr * std::sin(a));
                        if (i) dl->AddLine(prev, q, uCol, 1.6f); prev = q;
                    }
                };
                arcPoly(rad + hw); arcPoly(rad - hw);
                auto cap = [&](float aDeg) {
                    float sx = cu + rad * std::cos(aDeg * D2R), sy = cv + rad * std::sin(aDeg * D2R);
                    ImVec2 prev;
                    for (int i = 0; i <= 24; ++i) {
                        float t = (aDeg - 90.0f + 180.0f * (i / 24.0f)) * D2R;
                        ImVec2 q = uvToScreen(sx + hw * std::cos(t), sy + hw * std::sin(t));
                        if (i) dl->AddLine(prev, q, uCol, 1.6f); prev = q;
                    }
                };
                cap(a0d); cap(a1d);
            };
            // Straight-slot capsule preview: two side lines tangent to the end caps (parallel to
            // the spine, offset by the half-width) plus a 180-degree cap arc at each spine end.
            auto drawStraightSlot = [&](ImVec2 p1, ImVec2 p2, float hw) {
                float dx = p2.x - p1.x, dy = p2.y - p1.y; float L = std::hypot(dx, dy);
                if (L < 1e-6f) return;
                float nx = -dy / L, ny = dx / L;          // unit perpendicular to the spine
                dl->AddLine(uvToScreen(p1.x + nx * hw, p1.y + ny * hw),
                            uvToScreen(p2.x + nx * hw, p2.y + ny * hw), uCol, 1.6f);
                dl->AddLine(uvToScreen(p1.x - nx * hw, p1.y - ny * hw),
                            uvToScreen(p2.x - nx * hw, p2.y - ny * hw), uCol, 1.6f);
                auto cap = [&](ImVec2 c, float axis) {       // outward-facing 180-deg arc
                    ImVec2 prev;
                    for (int i = 0; i <= 24; ++i) {
                        float a = axis - 1.5707963f + 3.14159265f * (i / 24.0f);
                        ImVec2 q = uvToScreen(c.x + hw * std::cos(a), c.y + hw * std::sin(a));
                        if (i) dl->AddLine(prev, q, uCol, 1.6f); prev = q;
                    }
                };
                cap(p1, std::atan2(p1.y - p2.y, p1.x - p2.x));
                cap(p2, std::atan2(p2.y - p1.y, p2.x - p1.x));
                // centerline previewed as the (dashed) construction spine
                drawDashedLine(dl, uvToScreen(p1.x, p1.y), uvToScreen(p2.x, p2.y),
                               col(pal.sketchUnder, 0.7f), 1.0f, 6.0f, 4.0f);
            };
            const size_t n = toolUV_.size();
            if (tool_ == SketchTool::Rect3ptCorner && n >= 1) {
                ImVec2 A = toolUV_[0];
                ImVec2 B = (n >= 2) ? toolUV_[1] : ImVec2(su, sv);
                if (n < 2) { dl->AddLine(uvToScreen(A.x, A.y), sCur, uCol, 1.6f); }
                else {
                    // edge A-B fixed; third click sets width (perp offset of cursor from A-B).
                    float ex = B.x - A.x, ey = B.y - A.y; float L = std::hypot(ex, ey);
                    float nx = L > 1e-6f ? -ey / L : 0, ny = L > 1e-6f ? ex / L : 0;
                    float w = (su - A.x) * nx + (sv - A.y) * ny;
                    ImVec2 D(A.x + nx * w, A.y + ny * w), C(B.x + nx * w, B.y + ny * w);
                    pl({A, B, C, D, A});
                }
            } else if (tool_ == SketchTool::Rect3ptCenter && n >= 1) {
                ImVec2 ctr = toolUV_[0];
                ImVec2 mid = (n >= 2) ? toolUV_[1] : ImVec2(su, sv);
                if (n < 2) { dl->AddLine(uvToScreen(ctr.x, ctr.y), sCur, uCol, 1.6f); }
                else {
                    // ctr->mid is a half-edge direction (half-height); cursor sets half-width.
                    float hx = mid.x - ctr.x, hy = mid.y - ctr.y; float L = std::hypot(hx, hy);
                    float px = L > 1e-6f ? -hy / L : 0, py = L > 1e-6f ? hx / L : 0;
                    float hw = (su - ctr.x) * px + (sv - ctr.y) * py;
                    ImVec2 wv(px * hw, py * hw);
                    ImVec2 P0(ctr.x + hx + wv.x, ctr.y + hy + wv.y);
                    ImVec2 P1(ctr.x + hx - wv.x, ctr.y + hy - wv.y);
                    ImVec2 P2(ctr.x - hx - wv.x, ctr.y - hy - wv.y);
                    ImVec2 P3(ctr.x - hx + wv.x, ctr.y - hy + wv.y);
                    pl({P0, P1, P2, P3, P0});
                }
            } else if (tool_ == SketchTool::Parallelogram && n >= 1) {
                ImVec2 A = toolUV_[0];
                ImVec2 B = (n >= 2) ? toolUV_[1] : ImVec2(su, sv);
                if (n < 2) { dl->AddLine(uvToScreen(A.x, A.y), sCur, uCol, 1.6f); }
                else {
                    ImVec2 C(su, sv);                 // third corner sets the shear
                    ImVec2 D(A.x + (C.x - B.x), A.y + (C.y - B.y));
                    pl({A, B, C, D, A});
                }
            } else if (tool_ == SketchTool::CirclePerimeter) {
                std::vector<ImVec2> pts;
                for (size_t i = 0; i < n && i < 3; ++i) pts.push_back(toolUV_[i]);
                if (pts.size() < 3) pts.push_back(ImVec2(su, sv));
                for (auto& q : pts) dl->AddCircleFilled(uvToScreen(q.x, q.y), 3.0f, uCol);
                if (pts.size() == 3) {
                    float cu, cv, rad;
                    if (circumcircle(pts[0], pts[1], pts[2], cu, cv, rad)) {
                        ImVec2 prev;
                        for (int i = 0; i <= 48; ++i) {
                            float a = i / 48.0f * 6.2831853f;
                            ImVec2 q = uvToScreen(cu + rad * std::cos(a), cv + rad * std::sin(a));
                            if (i) dl->AddLine(prev, q, uCol, 1.6f); prev = q;
                        }
                    }
                }
            } else if (tool_ == SketchTool::Slot && n >= 2) {
                // spine fixed by clicks 1 & 2; cursor sets the width (perp distance from spine).
                ImVec2 p1 = toolUV_[0], p2 = toolUV_[1];
                float dx = p2.x - p1.x, dy = p2.y - p1.y; float L = std::hypot(dx, dy);
                float hw = L > 1e-6f ? std::fabs((su - p1.x) * (-dy) + (sv - p1.y) * dx) / L : 5.0f;
                if (hw < 1e-4f) hw = 1e-4f;
                drawStraightSlot(p1, p2, hw);
            } else if (tool_ == SketchTool::SlotCtrStr && n >= 1) {
                ImVec2 ctr = toolUV_[0];
                ImVec2 end = (n >= 2) ? toolUV_[1] : ImVec2(su, sv);
                if (n < 2) { dl->AddLine(uvToScreen(ctr.x, ctr.y), sCur, uCol, 1.6f); }
                else {
                    // centerpoint slot: spine endpoints are ctr +/- (end-ctr); cursor sets width.
                    ImVec2 e1(ctr.x + (end.x - ctr.x), ctr.y + (end.y - ctr.y));
                    ImVec2 e2(ctr.x - (end.x - ctr.x), ctr.y - (end.y - ctr.y));
                    float dx = e1.x - e2.x, dy = e1.y - e2.y; float L = std::hypot(dx, dy);
                    float hw = L > 1e-6f ? std::fabs((su - e2.x) * (-dy) + (sv - e2.y) * dx) / L : 5.0f;
                    if (hw < 1e-4f) hw = 1e-4f;
                    drawStraightSlot(e1, e2, hw);
                }
            } else if (tool_ == SketchTool::SlotArcCtr && n >= 1) {
                // click 1 = arc center; 2 = spine start (sets radius+a0); 3 = spine end (a1);
                // 4 = width. Live preview at each stage with the snapped cursor.
                const float R2D = 57.29577951f;
                ImVec2 ctr = toolUV_[0];
                dl->AddCircleFilled(uvToScreen(ctr.x, ctr.y), 3.0f, uCol);
                if (n < 2) { dl->AddLine(uvToScreen(ctr.x, ctr.y), sCur, uCol, 1.6f); }
                else {
                    ImVec2 s0 = toolUV_[1];
                    float rad = std::hypot(s0.x - ctr.x, s0.y - ctr.y);
                    float a0 = std::atan2(s0.y - ctr.y, s0.x - ctr.x) * R2D;
                    ImVec2 endPt = (n >= 3) ? toolUV_[2] : ImVec2(su, sv);
                    float a1 = std::atan2(endPt.y - ctr.y, endPt.x - ctr.x) * R2D;
                    float hw = 0.0f;
                    if (n >= 3) {  // width stage: perp distance of cursor from the spine radius
                        hw = std::fabs(std::hypot(su - ctr.x, sv - ctr.y) - rad);
                        if (hw < 1e-3f) hw = rad * 0.15f;
                    } else { hw = rad * 0.15f; }
                    drawCapsule(ctr.x, ctr.y, rad, a0, a1, hw);
                }
            } else if (tool_ == SketchTool::SlotArc3pt && n >= 1) {
                // clicks 1,2,3 = points ON the spine arc (circumcircle); 4 = width.
                const float R2D = 57.29577951f;
                std::vector<ImVec2> pts;
                for (size_t i = 0; i < n && i < 3; ++i) pts.push_back(toolUV_[i]);
                if (pts.size() < 3) pts.push_back(ImVec2(su, sv));
                for (auto& q : pts) dl->AddCircleFilled(uvToScreen(q.x, q.y), 3.0f, uCol);
                float cu, cv, rad;
                if (circumcircle(pts[0], pts[1], pts[2], cu, cv, rad)) {
                    float a0 = std::atan2(pts[0].y - cv, pts[0].x - cu) * R2D;
                    float amid = std::atan2(pts[1].y - cv, pts[1].x - cu) * R2D;
                    float a1 = std::atan2(pts[2].y - cv, pts[2].x - cu) * R2D;
                    // Choose CCW a0->a1 that passes through the middle pick.
                    auto norm = [](float a) { while (a < 0) a += 360.0f; while (a >= 360.0f) a -= 360.0f; return a; };
                    float sweep = norm(a1 - a0), midSw = norm(amid - a0);
                    if (midSw > sweep) { float t = a0; a0 = a1; a1 = t; }  // go the other way
                    float hw = (n >= 3) ? std::fabs(std::hypot(su - cu, sv - cv) - rad) : rad * 0.15f;
                    if (hw < 1e-3f) hw = rad * 0.15f;
                    drawCapsule(cu, cv, rad, a0, a1, hw);
                }
            } else if (tool_ == SketchTool::EllipsePartial && n >= 1) {
                ImVec2 ctr = toolUV_[0];
                ImVec2 rxp = (n >= 2) ? toolUV_[1] : ImVec2(su, sv);
                float rot = std::atan2(rxp.y - ctr.y, rxp.x - ctr.x);
                float rx = std::hypot(rxp.x - ctr.x, rxp.y - ctr.y);
                float ca = std::cos(rot), sa = std::sin(rot);
                if (n < 2) { dl->AddLine(uvToScreen(ctr.x, ctr.y), sCur, uCol, 1.6f); }
                else {
                    // ry from the third pick's perpendicular distance; live cursor while n==2.
                    ImVec2 ryp = (n >= 3) ? toolUV_[2] : ImVec2(su, sv);
                    float ry = std::fabs(-(ryp.x - ctr.x) * sa + (ryp.y - ctr.y) * ca);
                    if (ry < 1e-4f) ry = rx * 0.5f;
                    // angle sweep: a0 from the 4th pick, a1 from the 5th/live pick.
                    auto angOf = [&](ImVec2 q) {
                        float lx = (q.x - ctr.x) * ca + (q.y - ctr.y) * sa;
                        float ly = -(q.x - ctr.x) * sa + (q.y - ctr.y) * ca;
                        return std::atan2(ly / std::fmax(ry, 1e-6f), lx / std::fmax(rx, 1e-6f));
                    };
                    float a0 = (n >= 4) ? angOf(toolUV_[3]) : 0.0f;
                    float a1 = (n >= 4) ? angOf(ImVec2(su, sv)) : 6.2831853f;
                    if (a1 < a0) a1 += 6.2831853f;
                    ImVec2 prev;
                    for (int i = 0; i <= 64; ++i) {
                        float t = a0 + (a1 - a0) * (i / 64.0f);
                        float lx = rx * std::cos(t), ly = ry * std::sin(t);
                        ImVec2 q = uvToScreen(ctr.x + lx * ca - ly * sa, ctr.y + lx * sa + ly * ca);
                        if (i) dl->AddLine(prev, q, uCol, 1.6f); prev = q;
                    }
                }
            } else if (tool_ == SketchTool::Parabola && n >= 1) {
                ImVec2 focus = toolUV_[0];
                ImVec2 vertex = (n >= 2) ? toolUV_[1] : ImVec2(su, sv);
                dl->AddCircleFilled(uvToScreen(focus.x, focus.y), 3.0f, uCol);
                if (n < 2) { dl->AddLine(uvToScreen(focus.x, focus.y), sCur, uCol, 1.6f); }
                else {
                    // axis = vertex->focus; f = |vertex-focus|. Sample y = x^2/(4f) in axis frame
                    // over the [start..end] parametric range set by the 3rd/4th (or live) picks.
                    float ax = focus.x - vertex.x, ay = focus.y - vertex.y;
                    float f = std::hypot(ax, ay); if (f < 1e-4f) f = 1.0f;
                    float ux = ax / f, uy = ay / f;     // axis dir
                    float px = -uy, py = ux;            // perp (opening) dir
                    auto tOf = [&](ImVec2 q) { return (q.x - vertex.x) * px + (q.y - vertex.y) * py; };
                    float t0 = (n >= 3) ? tOf(toolUV_[2]) : -2.0f * f;
                    float t1 = (n >= 3) ? tOf(ImVec2(su, sv)) : 2.0f * f;
                    ImVec2 prev;
                    for (int i = 0; i <= 48; ++i) {
                        float t = t0 + (t1 - t0) * (i / 48.0f);
                        float d = t * t / (4.0f * f);    // distance along axis from vertex
                        ImVec2 q = uvToScreen(vertex.x + ux * d + px * t, vertex.y + uy * d + py * t);
                        if (i) dl->AddLine(prev, q, uCol, 1.6f); prev = q;
                    }
                }
            } else if (tool_ == SketchTool::Conic && n >= 1) {
                ImVec2 P0 = toolUV_[0];
                ImVec2 P1 = (n >= 2) ? toolUV_[1] : ImVec2(su, sv);
                dl->AddCircleFilled(uvToScreen(P0.x, P0.y), 3.0f, uCol);
                if (n < 2) { dl->AddLine(uvToScreen(P0.x, P0.y), sCur, uCol, 1.6f); }
                else {
                    ImVec2 apex = (n >= 3) ? toolUV_[2] : ImVec2(su, sv);
                    // rho: 0..1 weight of the apex (shoulder). Live-pick while n==3 (cursor sets it),
                    // committed in conicRho_ once the apex is placed.
                    float rho = conicRho_;
                    if (n == 3) {  // live rho preview: project cursor onto the mid->apex segment
                        ImVec2 mid((P0.x + P1.x) * 0.5f, (P0.y + P1.y) * 0.5f);
                        float dx = apex.x - mid.x, dy = apex.y - mid.y; float L2 = dx * dx + dy * dy;
                        rho = L2 > 1e-9f ? ((su - mid.x) * dx + (sv - mid.y) * dy) / L2 : 0.5f;
                        rho = std::fmax(0.01f, std::fmin(0.99f, rho));
                    }
                    // Rational Bezier conic: B(t)=((1-t)^2 P0 + 2(1-t)t w Apex + t^2 P1)/den,
                    // den=(1-t)^2 + 2(1-t)t w + t^2, with w = rho/(1-rho).
                    float w = rho / std::fmax(1e-3f, 1.0f - rho);
                    ImVec2 prev;
                    for (int i = 0; i <= 48; ++i) {
                        float t = i / 48.0f, mt = 1.0f - t;
                        float b0 = mt * mt, b1 = 2.0f * mt * t * w, b2 = t * t;
                        float den = b0 + b1 + b2;
                        float qx = (b0 * P0.x + b1 * apex.x + b2 * P1.x) / den;
                        float qy = (b0 * P0.y + b1 * apex.y + b2 * P1.y) / den;
                        ImVec2 q = uvToScreen(qx, qy);
                        if (i) dl->AddLine(prev, q, uCol, 1.6f); prev = q;
                    }
                    dl->AddCircle(uvToScreen(apex.x, apex.y), 4.0f, uCol, 12, 1.2f);
                }

            // ---- C42 tool previews ----
            } else if (tool_ == SketchTool::Arc3pt && n >= 1) {
                // picks: arc start, arc end, point ON the arc (live cursor fills the missing one).
                std::vector<ImVec2> pts;
                for (size_t i = 0; i < n && i < 3; ++i) pts.push_back(toolUV_[i]);
                if (pts.size() < 3) pts.push_back(ImVec2(su, sv));
                for (auto& q : pts) dl->AddCircleFilled(uvToScreen(q.x, q.y), 3.0f, uCol);
                if (n == 1) { dl->AddLine(uvToScreen(pts[0].x, pts[0].y), sCur, uCol, 1.6f); }
                else {
                    float cu, cv, rad;
                    if (circumcircle(pts[0], pts[1], pts[2], cu, cv, rad)) {
                        const float R2D = 57.29577951f;
                        float aS = std::atan2(pts[0].y - cv, pts[0].x - cu) * R2D;
                        float aE = std::atan2(pts[1].y - cv, pts[1].x - cu) * R2D;
                        float aM = std::atan2(pts[2].y - cv, pts[2].x - cu) * R2D;
                        auto norm = [](float a) { while (a < 0) a += 360.0f; while (a >= 360.0f) a -= 360.0f; return a; };
                        bool fwd = norm(aM - aS) <= norm(aE - aS);   // CCW start->end passes thru mid?
                        float a0 = fwd ? aS : aE, a1 = fwd ? aE : aS;
                        if (a1 < a0) a1 += 360.0f;
                        const float D2R = 3.14159265f / 180.0f;
                        ImVec2 prev;
                        for (int i = 0; i <= 48; ++i) {
                            float a = (a0 + (a1 - a0) * (i / 48.0f)) * D2R;
                            ImVec2 q = uvToScreen(cu + rad * std::cos(a), cv + rad * std::sin(a));
                            if (i) dl->AddLine(prev, q, uCol, 1.6f); prev = q;
                        }
                    }
                }
            } else if (tool_ == SketchTool::ArcTangent) {
                // C54: derive the source + free endpoint each frame; the arc starts there tangent to
                // the source and sweeps to the live cursor. Highlight the snapped start; if no source
                // exists we fall back to a plain 2-point chord (treated as a near-straight arc).
                TangentArcSrc src;
                if (tangentArcSource(io.MousePos, src)) {
                    // bright dot on the tangent start + faint tangent direction tick
                    ImVec2 sS = uvToScreen(src.start.x, src.start.y);
                    dl->AddCircleFilled(sS, 4.0f, col(pal.accent, 0.95f));
                    ImVec2 tEnd = uvToScreen(src.start.x + src.tdir.x * 6.0f,
                                             src.start.y + src.tdir.y * 6.0f);
                    dl->AddLine(sS, tEnd, col(pal.accent, 0.6f), 1.2f);
                    float cu, cv, rad, a0, a1; bool ccw;
                    if (tangentArcThrough(src.start, src.tdir, ImVec2(su, sv),
                                          cu, cv, rad, a0, a1, ccw)) {
                        if (a1 < a0) a1 += 360.0f;
                        float lo = ccw ? a0 : a1, hi = ccw ? a1 : a0;
                        if (hi < lo) hi += 360.0f;
                        const float D2R = 3.14159265f / 180.0f;
                        ImVec2 prev;
                        for (int i = 0; i <= 48; ++i) {
                            float a = (lo + (hi - lo) * (i / 48.0f)) * D2R;
                            ImVec2 q = uvToScreen(cu + rad * std::cos(a), cv + rad * std::sin(a));
                            if (i) dl->AddLine(prev, q, uCol, 1.6f); prev = q;
                        }
                    } else {
                        // Degenerate (cursor on the tangent line): preview a straight chord.
                        dl->AddLine(sS, sCur, uCol, 1.6f);
                    }
                } else {
                    // No source -> behave like a 3-pt arc's first leg (rubber chord from cursor).
                    dl->AddCircleFilled(sCur, 3.0f, uCol);
                }
            } else if (tool_ == SketchTool::Polygon && n >= 1) {
                ImVec2 ctr = toolUV_[0];
                dl->AddCircleFilled(uvToScreen(ctr.x, ctr.y), 3.0f, uCol);
                float vx = su - ctr.x, vy = sv - ctr.y;
                float R = std::hypot(vx, vy);
                if (R < 1e-4f) { dl->AddLine(uvToScreen(ctr.x, ctr.y), sCur, uCol, 1.6f); }
                else {
                    float a0 = std::atan2(vy, vx);
                    ImVec2 prev;
                    for (int i = 0; i <= polySides_; ++i) {
                        float a = a0 + 6.2831853f * i / (float)polySides_;
                        ImVec2 q = uvToScreen(ctr.x + R * std::cos(a), ctr.y + R * std::sin(a));
                        if (i) dl->AddLine(prev, q, uCol, 1.6f); prev = q;
                    }
                }
            } else if (tool_ == SketchTool::Star && n >= 1) {
                ImVec2 ctr = toolUV_[0];
                dl->AddCircleFilled(uvToScreen(ctr.x, ctr.y), 3.0f, uCol);
                float vx = su - ctr.x, vy = sv - ctr.y;
                float R = std::hypot(vx, vy);
                if (R < 1e-4f) { dl->AddLine(uvToScreen(ctr.x, ctr.y), sCur, uCol, 1.6f); }
                else {
                    float a0 = std::atan2(vy, vx);
                    float Ri = R * polyStarRatio_;
                    int pts = polyStarPoints_;
                    ImVec2 prev;
                    for (int i = 0; i <= 2 * pts; ++i) {
                        float a = a0 + (float)i * 3.14159265f / (float)pts;
                        float r = (i & 1) ? Ri : R;
                        ImVec2 q = uvToScreen(ctr.x + r * std::cos(a), ctr.y + r * std::sin(a));
                        if (i) dl->AddLine(prev, q, uCol, 1.6f); prev = q;
                    }
                }
            } else if (tool_ == SketchTool::RectCenter && n >= 1) {
                ImVec2 ctr = toolUV_[0];
                dl->AddCircleFilled(uvToScreen(ctr.x, ctr.y), 3.0f, uCol);
                float dx = std::fabs(su - ctr.x), dy = std::fabs(sv - ctr.y);
                ImVec2 A(ctr.x - dx, ctr.y - dy), B(ctr.x + dx, ctr.y - dy);
                ImVec2 C(ctr.x + dx, ctr.y + dy), D(ctr.x - dx, ctr.y + dy);
                pl({A, B, C, D, A});
            } else if (tool_ == SketchTool::Ellipse && n >= 1) {
                ImVec2 ctr = toolUV_[0];
                ImVec2 rxp = (n >= 2) ? toolUV_[1] : ImVec2(su, sv);
                float rot = std::atan2(rxp.y - ctr.y, rxp.x - ctr.x);
                float rx = std::hypot(rxp.x - ctr.x, rxp.y - ctr.y);
                float ca = std::cos(rot), sa = std::sin(rot);
                if (n < 2) { dl->AddLine(uvToScreen(ctr.x, ctr.y), sCur, uCol, 1.6f); }
                else {
                    float ry = std::fabs(-(su - ctr.x) * sa + (sv - ctr.y) * ca);
                    if (ry < 1e-4f) ry = rx * 0.5f;
                    ImVec2 prev;
                    for (int i = 0; i <= 64; ++i) {
                        float a = i / 64.0f * 6.2831853f;
                        float lx = rx * std::cos(a), ly = ry * std::sin(a);
                        ImVec2 q = uvToScreen(ctr.x + lx * ca - ly * sa, ctr.y + lx * sa + ly * ca);
                        if (i) dl->AddLine(prev, q, uCol, 1.6f); prev = q;
                    }
                }
            } else if ((tool_ == SketchTool::Spline || tool_ == SketchTool::StyleSpline) && n >= 1) {
                std::vector<ImVec2> pts(toolUV_.begin(), toolUV_.end());
                pts.push_back(ImVec2(su, sv));
                pl(pts);
                for (auto& q : pts) dl->AddCircleFilled(uvToScreen(q.x, q.y), 3.0f, uCol);
            }
        }

        // ---- C49: inference feedback (dashed alignment lines, snap markers, relation chips) ----
        const ImU32 inferCol = col(pal.sketchInfer);
        if (infer.alignU || infer.alignV) {
            // Dashed line from the alignment source point to the cursor (positional only).
            auto dashed = [&](ImVec2 a, ImVec2 b) {
                float dx = b.x - a.x, dy = b.y - a.y;
                float L = std::hypot(dx, dy);
                if (L < 1.0f) return;
                const float dash = 6.0f * ui::sketch::uiScale(), gap = 4.0f * ui::sketch::uiScale();
                for (float t = 0; t < L; t += dash + gap) {
                    float e = std::fmin(t + dash, L);
                    dl->AddLine(ImVec2(a.x + dx * (t / L), a.y + dy * (t / L)),
                                ImVec2(a.x + dx * (e / L), a.y + dy * (e / L)), inferCol, 1.2f);
                }
            };
            if (infer.alignU) dashed(uvToScreen(infer.srcU.x, infer.srcU.y), sCur);
            if (infer.alignV) dashed(uvToScreen(infer.srcV.x, infer.srcV.y), sCur);
        }
        // Cursor marker.
        dl->AddCircle(sCur, 4.0f * ui::sketch::uiScale(), prevCol, 12, 1.5f);
        // Snap markers (unified, ui::sketch::uiScale()-scaled): yellow-ish dot = endpoint/point snap,
        // concentric rings = center snap, diamond = midpoint snap, plain ring = rim contact.
        if (!snapH.empty()) {
            const float ms = 5.0f * ui::sketch::uiScale();
            if (snapHasPrefix(snapH, "mid:")) {
                ImVec2 d[4] = {ImVec2(sCur.x, sCur.y - ms * 1.5f), ImVec2(sCur.x + ms * 1.5f, sCur.y),
                               ImVec2(sCur.x, sCur.y + ms * 1.5f), ImVec2(sCur.x - ms * 1.5f, sCur.y)};
                dl->AddQuad(d[0], d[1], d[2], d[3], inferCol, 1.6f);
            } else if (snapHasPrefix(snapH, "rim:") || snapHasPrefix(snapH, "tan:")) {
                dl->AddCircle(sCur, ms * 1.4f, inferCol, 16, 1.6f);
            } else if (snapH.find(".center") != std::string::npos) {
                dl->AddCircle(sCur, ms, inferCol, 16, 1.6f);
                dl->AddCircle(sCur, ms * 1.9f, inferCol, 16, 1.2f);
            } else {  // endpoint / point / spline through-point
                dl->AddCircleFilled(sCur, ms * 0.7f, inferCol);
                dl->AddCircle(sCur, ms * 1.4f, inferCol, 16, 1.2f);
            }
        }
        // Live relation chips near the cursor (same style as the C41 relation badges).
        if (hvH || hvV || tanSnap) {
            const float bs2 = 16.0f * ui::sketch::uiScale();
            float chipX = sCur.x + bs2;                       // below-right of the cursor
            auto chip = [&](const char* glyph) {
                ImVec2 gp(chipX, sCur.y + bs2);
                ImVec2 c0(gp.x - bs2 * 0.5f, gp.y - bs2 * 0.5f), c1(gp.x + bs2 * 0.5f, gp.y + bs2 * 0.5f);
                dl->AddRectFilled(c0, c1, col(pal.sketchRelBg), 3.5f * ui::sketch::uiScale());
                dl->AddRect(c0, c1, col(pal.sketchRelBorder), 3.5f * ui::sketch::uiScale(), 0, 1.2f);
                ImVec2 gs = ImGui::CalcTextSize(glyph);
                dl->AddText(ImVec2(gp.x - gs.x * 0.5f, gp.y - gs.y * 0.5f), col(pal.sketchInk), glyph);
                chipX += bs2 + 2.0f;
            };
            if (hvH) chip("-");
            if (hvV) chip("|");
            if (tanSnap) chip("T");
        }

        // Click placement. EqCurve takes no viewport pick (origin-relative; its popup opens on the
        // ribbon command). Text takes exactly one placement click and then defers to its popup, so
        // suppress further captures while that popup is open.
        if (imgHovered && ImGui::IsMouseClicked(ImGuiMouseButton_Left) &&
            tool_ != SketchTool::EqCurve && tool_ != SketchTool::Airfoil &&
            !(tool_ == SketchTool::Text && textPopupOpen_)) {
            // When no point snap fired but the click landed on a v/h alignment guide, record the
            // alignment so the commit can write it as a real vertical/horizontal relation (the new
            // point's handle is filled in per-tool via pushSnapConstraint). Positional-only if the
            // toggle is off — then this stays "" and only the pulled coordinate persists.
            std::string clickSnap = snapH;
            if (clickSnap.empty() && autoAlignRelations_ && inferenceOn_ &&
                (infer.alignU || infer.alignV)) {
                std::string uH = infer.alignU ? infer.srcUHandle : std::string();
                std::string vH = infer.alignV ? infer.srcVHandle : std::string();
                if (!uH.empty() || !vH.empty()) clickSnap = "align:" + uH + "|" + vH;
            }
            toolUV_.push_back(ImVec2(su, sv));
            toolSnaps_.push_back(clickSnap);
            // Sketch Text — the single click captures the lower-left anchor; open the popup that
            // collects the string + cap height. The entity is emitted from the popup's OK.
            if (tool_ == SketchTool::Text && toolUV_.size() == 1) {
                textAnchorUV_ = toolUV_[0];
                textPopupPos_ = io.MousePos;
                textPopupOpen_ = true;
                textFocus_ = true;
                toolUV_.clear(); toolSnaps_.clear();   // anchor stashed; stop click capture
            } else
            // Tools that complete on a fixed number of clicks:
            // C54: Tangent Arc — ONE click places the end. The start + tangent come from the source
            // entity (resolved at click time). We commit an arc tangent to the source there, joined
            // by a coincident at the shared endpoint and a tangent relation (arc <-> source) so it
            // stays tangent under drag. No source / degenerate geometry -> toast a hint and reset.
            if (tool_ == SketchTool::ArcTangent && toolUV_.size() == 1) {
                ImVec2 endPt = toolUV_[0];
                TangentArcSrc src;
                float cu, cv, rad, a0, a1; bool ccw;
                if (!tangentArcSource(io.MousePos, src)) {
                    st.toast(ui::sketch::ToastLevel::Info,
                             "Tangent Arc: draw or select a line/arc first, then click the arc end");
                    resetTool(); tool_ = SketchTool::ArcTangent;
                } else if (!tangentArcThrough(src.start, src.tdir, endPt,
                                              cu, cv, rad, a0, a1, ccw)) {
                    st.toast(ui::sketch::ToastLevel::Info,
                             "Tangent Arc: the end is on the tangent line (no arc) — pick a point off it");
                    resetTool(); tool_ = SketchTool::ArcTangent;
                } else {
                    // The solved arc always sweeps start->end; store CCW with the matching angle order
                    // (solver drops the ccw flag, so a false flag would re-render mirrored). When the
                    // geometric sweep is CW, store the CCW arc end->start (same curve) and join the
                    // source endpoint to whichever arc handle landed on the start.
                    float sStart = a0, sEnd = a1;
                    const char* startHandle = "$self.start";
                    if (!ccw) { float t = sStart; sStart = sEnd; sEnd = t; startHandle = "$self.end"; }
                    nlohmann::json ent = {{"type", "arc"},
                        {"center", nlohmann::json::array({cu, cv})}, {"radius", rad},
                        {"start", sStart}, {"end", sEnd}, {"ccw", true}};
                    // Relations: coincident joining the source endpoint to the arc's start handle +
                    // tangent(arc, source). For a whole-circle source there's no endpoint sub-handle
                    // (src.handle == src.id) so we skip the coincident and keep only the tangent.
                    nlohmann::json cs = nlohmann::json::array();
                    if (src.handle != src.id)
                        cs.push_back({{"type", "coincident"}, {"refs", {src.handle, startHandle}}});
                    cs.push_back({{"type", "tangent"}, {"refs", {"$self", src.id}}});
                    addEntity(st, ent, cs);
                    resetTool(); tool_ = SketchTool::ArcTangent;   // stay armed for another tangent arc
                }
            } else if (tool_ == SketchTool::Line && toolUV_.size() == 2) {
                ImVec2 p1 = toolUV_[0], p2 = toolUV_[1];
                nlohmann::json cs = nlohmann::json::array();
                // C49: a rim-snapped START whose placement direction matched gets the tangent too.
                if (tanStartUpgrade && snapHasPrefix(toolSnaps_[0], "rim:"))
                    toolSnaps_[0] = "tan:" + toolSnaps_[0].substr(4);
                bool tangentSeg = snapHasPrefix(toolSnaps_[0], "tan:") ||
                                  snapHasPrefix(toolSnaps_[1], "tan:");
                // Auto h/v relation (inference only). When the END landed on a snapped point that
                // point wins: only add h/v if the result is EXACTLY axis-aligned. A tangent-snapped
                // direction never gets h/v stacked on top.
                float du = p2.x - p1.x, dv = p2.y - p1.y;
                if (inferenceOn_ && !tangentSeg) {
                    float ang = std::atan2(std::fabs(dv), std::fabs(du)) * 57.2957795f;
                    float tol = toolSnaps_[1].empty() ? kHVDeg : 0.01f;
                    if (ang < tol) cs.push_back({{"type", "horizontal"}, {"refs", {"$self"}}});
                    else if (ang > 90.0f - tol) cs.push_back({{"type", "vertical"}, {"refs", {"$self"}}});
                }
                // C49: snap-derived constraint for one endpoint (pt = "$self.p1"/"$self.p2"):
                // plain handle -> coincident; "mid:" -> midpoint; "rim:"/"tan:" -> point_on
                // (+ one tangent per segment).
                bool tangentEmitted = false;
                auto snapCons = [&](const std::string& snap, const char* pt) {
                    if (snap.empty()) return;
                    if (snapHasPrefix(snap, "align:")) { pushSnapConstraint(snap, pt, cs); return; }
                    if (snapHasPrefix(snap, "mid:")) {
                        cs.push_back({{"type", "midpoint"}, {"refs", {pt, snap.substr(4)}}});
                    } else if (snapHasPrefix(snap, "rim:") || snapHasPrefix(snap, "tan:")) {
                        std::string eid = snap.substr(4);
                        cs.push_back({{"type", "point_on"}, {"refs", {pt, eid}}});
                        if (snapHasPrefix(snap, "tan:") && !tangentEmitted) {
                            cs.push_back({{"type", "tangent"}, {"refs", {"$self", eid}}});
                            tangentEmitted = true;
                        }
                    } else {
                        cs.push_back({{"type", "coincident"}, {"refs", {snap, pt}}});
                    }
                };
                // C37-D: start endpoint — from the chained predecessor's p2 (so a continued chain
                // stays constrained) OR from the start click's snap.
                if (!chainPrevId_.empty())
                    cs.push_back({{"type", "coincident"}, {"refs", {chainPrevId_ + ".p2", "$self.p1"}}});
                else
                    snapCons(toolSnaps_[0], "$self.p1");
                // C49: the END click's snap is now constrained too (was positional-only before).
                snapCons(toolSnaps_[1], "$self.p2");
                nlohmann::json ent = {{"type", "line"},
                    {"p1", nlohmann::json::array({p1.x, p1.y})},
                    {"p2", nlohmann::json::array({p2.x, p2.y})}};
                // Apply directly so we can capture the new line id for the next chain segment.
                nlohmann::json op = {{"op", "sketch_add_entity"}, {"sketch", fid_},
                                     {"entity", ent}, {"constraints", cs}};
                auto r = st.docMirror().applyOps(st.worker(), nlohmann::json::array({op}));
                std::string newId;
                if (!r.ok) {
                    st.toast(ui::sketch::ToastLevel::Error, "Line: " + r.error_message);
                } else {
                    if (!r.aicm_path.empty()) { std::string le; st.loadDocMesh(r.aicm_path, le); }
                    newId = (r.id_map.is_object() && r.id_map.contains("$0"))
                          ? r.id_map["$0"].get<std::string>() : std::string();
                    resolve(st);
                }
                // Chain: keep drawing from the new endpoint, remembering this segment's id so the
                // next segment's p1 gets a coincident to its p2.
                ImVec2 chainStart = p2;
                resetTool(); tool_ = SketchTool::Line;
                chainPrevId_ = newId;
                toolUV_.push_back(chainStart);
                toolSnaps_.push_back("");
            } else if (tool_ == SketchTool::RectCorner && toolUV_.size() == 2) {
                ImVec2 a = toolUV_[0], b = toolUV_[1];
                // Axis-aligned rect: 4 coincident-chained lines with per-edge h/v relations.
                // C50: route through addQuadLoop so the 4 line batches COALESCE into one undo step
                // (was an inline loop with 4 separate undo steps -> "vanishes one corner at a time").
                ImVec2 quad[4] = {{a.x, a.y}, {b.x, a.y}, {b.x, b.y}, {a.x, b.y}};
                const char* rel[4] = {"horizontal", "vertical", "horizontal", "vertical"};
                // The two clicked corners are quad[0] (first pick) and quad[2] (opposite); keep their
                // placement snaps so a corner dropped on the origin/a vertex stays coincident.
                std::string cornerSnaps[4] = {toolSnaps_[0], "", toolSnaps_[1], ""};
                addQuadLoop(st, quad, rel, cornerSnaps);
                resetTool();
            } else if (tool_ == SketchTool::Circle && toolUV_.size() == 2) {
                ImVec2 c = toolUV_[0], rim = toolUV_[1];
                float rad = std::hypot(rim.x - c.x, rim.y - c.y);
                nlohmann::json ent = {{"type", "circle"},
                    {"center", nlohmann::json::array({c.x, c.y})}, {"radius", rad}};
                // Persist the placement snaps: center -> $self.center; the rim click's snapped
                // point (a plain vertex) lies ON the circle (point_on). A rim snap onto another
                // curve at the radius pick is left positional (ambiguous curve-curve relation).
                nlohmann::json cs = nlohmann::json::array();
                pushSnapConstraint(toolSnaps_[0], "$self.center", cs);
                if (!toolSnaps_[1].empty() && !snapHasPrefix(toolSnaps_[1], "mid:") &&
                    !snapHasPrefix(toolSnaps_[1], "rim:") && !snapHasPrefix(toolSnaps_[1], "tan:"))
                    cs.push_back({{"type", "point_on"}, {"refs", {toolSnaps_[1], "$self"}}});
                addEntity(st, ent, cs);
                resetTool();
            } else if (tool_ == SketchTool::ArcCenter && toolUV_.size() == 3) {
                ImVec2 c = toolUV_[0], p1 = toolUV_[1], p2 = toolUV_[2];
                float rad = std::hypot(p1.x - c.x, p1.y - c.y);
                float a0 = std::atan2(p1.y - c.y, p1.x - c.x) * 57.2957795f;
                float a1 = std::atan2(p2.y - c.y, p2.x - c.x) * 57.2957795f;
                nlohmann::json ent = {{"type", "arc"},
                    {"center", nlohmann::json::array({c.x, c.y})}, {"radius", rad},
                    {"start", a0}, {"end", a1}, {"ccw", true}};
                // Persist placement snaps: center pick -> $self.center, the two rim picks ->
                // $self.start / $self.end (p1 defines the radius, p2 the end angle; both stay CCW).
                nlohmann::json cs = nlohmann::json::array();
                pushSnapConstraint(toolSnaps_[0], "$self.center", cs);
                pushSnapConstraint(toolSnaps_[1], "$self.start", cs);
                pushSnapConstraint(toolSnaps_[2], "$self.end", cs);
                addEntity(st, ent, cs);
                resetTool();
            } else if (tool_ == SketchTool::Slot && toolUV_.size() == 3) {
                ImVec2 p1 = toolUV_[0], p2 = toolUV_[1], wpt = toolUV_[2];
                // width = perpendicular distance from wpt to the p1-p2 line.
                float dx = p2.x - p1.x, dy = p2.y - p1.y;
                float L = std::hypot(dx, dy);
                float w = L > 1e-6f ? std::fabs((wpt.x - p1.x) * (-dy) + (wpt.y - p1.y) * dx) / L : 5.0f;
                if (w < 1e-3f) w = 5.0f;
                // C51: emit a decomposed slot (2 lines + 2 arcs + construction centerline) so every
                // piece is individually selectable / dimensionable. The two spine endpoints carry
                // their placement snaps (toolSnaps_[0]/[1]); the width pick (toolSnaps_[2]) doesn't.
                addDecomposedSlot(st, p1, p2, w * 2.0f, toolSnaps_[0], toolSnaps_[1]);
                resetTool();

            // ---- C28-B variants ----
            } else if (tool_ == SketchTool::Rect3ptCorner && toolUV_.size() == 3) {
                // A, B (one edge + angle), C (width by perp offset). Rotated rect: opposite sides
                // parallel+equal, adjacent perpendicular.
                ImVec2 A = toolUV_[0], B = toolUV_[1], C3 = toolUV_[2];
                float ex = B.x - A.x, ey = B.y - A.y; float L = std::hypot(ex, ey);
                float nx = L > 1e-6f ? -ey / L : 0, ny = L > 1e-6f ? ex / L : 0;
                float w = (C3.x - A.x) * nx + (C3.y - A.y) * ny;
                ImVec2 D(A.x + nx * w, A.y + ny * w), C(B.x + nx * w, B.y + ny * w);
                ImVec2 quad[4] = {A, B, C, D};
                const char* rel[4] = {"parallel perpendicular", "parallel", nullptr, nullptr};
                addQuadLoop(st, quad, rel);
                resetTool();
            } else if (tool_ == SketchTool::Rect3ptCenter && toolUV_.size() == 3) {
                // center, edge-midpoint dir (half-height), half-width pick.
                ImVec2 ctr = toolUV_[0], mid = toolUV_[1], wpk = toolUV_[2];
                float hx = mid.x - ctr.x, hy = mid.y - ctr.y; float L = std::hypot(hx, hy);
                float px = L > 1e-6f ? -hy / L : 0, py = L > 1e-6f ? hx / L : 0;
                float hw = (wpk.x - ctr.x) * px + (wpk.y - ctr.y) * py;
                ImVec2 wv(px * hw, py * hw);
                ImVec2 P0(ctr.x + hx + wv.x, ctr.y + hy + wv.y);
                ImVec2 P1(ctr.x + hx - wv.x, ctr.y + hy - wv.y);
                ImVec2 P2(ctr.x - hx - wv.x, ctr.y - hy - wv.y);
                ImVec2 P3(ctr.x - hx + wv.x, ctr.y - hy + wv.y);
                ImVec2 quad[4] = {P0, P1, P2, P3};
                const char* rel[4] = {"parallel perpendicular", "parallel", nullptr, nullptr};
                addQuadLoop(st, quad, rel);
                resetTool();
            } else if (tool_ == SketchTool::Parallelogram && toolUV_.size() == 3) {
                // 3 corners -> opposite sides parallel+equal, NO perpendicular.
                ImVec2 A = toolUV_[0], B = toolUV_[1], C = toolUV_[2];
                ImVec2 D(A.x + (C.x - B.x), A.y + (C.y - B.y));
                ImVec2 quad[4] = {A, B, C, D};
                const char* rel[4] = {"parallel equal", "parallel equal", nullptr, nullptr};
                addQuadLoop(st, quad, rel);
                resetTool();
            } else if (tool_ == SketchTool::CirclePerimeter && toolUV_.size() == 3) {
                float cu, cv, rad;
                if (!circumcircle(toolUV_[0], toolUV_[1], toolUV_[2], cu, cv, rad)) {
                    // collinear/degenerate guard: drop the 3rd pick, keep first two, hint via log.
                    st.addLog("[sketch] Perimeter Circle: 3 points are collinear — pick a 3rd off the line");
                    toolUV_.pop_back(); toolSnaps_.pop_back();
                } else {
                    nlohmann::json ent = {{"type", "circle"},
                        {"center", nlohmann::json::array({cu, cv})}, {"radius", rad}};
                    addEntity(st, ent, nlohmann::json::array());
                    resetTool();
                }
            } else if (tool_ == SketchTool::SlotCtrStr && toolUV_.size() == 3) {
                // center, end-direction/length, width. Spine = center +/- (end-center).
                ImVec2 ctr = toolUV_[0], end = toolUV_[1], wpk = toolUV_[2];
                ImVec2 e1(ctr.x + (end.x - ctr.x), ctr.y + (end.y - ctr.y));
                ImVec2 e2(ctr.x - (end.x - ctr.x), ctr.y - (end.y - ctr.y));
                float dx = e1.x - e2.x, dy = e1.y - e2.y; float L = std::hypot(dx, dy);
                float w = L > 1e-6f ? std::fabs((wpk.x - e2.x) * (-dy) + (wpk.y - e2.y) * dx) / L : 5.0f;
                if (w < 1e-3f) w = 5.0f;
                // C51: decomposed slot (real lines + arcs + construction centerline). e1 is the
                // clicked end (carries toolSnaps_[1]); e2 is its mirror about the center (no pick).
                addDecomposedSlot(st, e1, e2, w * 2.0f, toolSnaps_[1], "");
                resetTool();
            } else if (tool_ == SketchTool::SlotArcCtr && toolUV_.size() == 4) {
                // center, spine start (radius+a0), spine end dir (a1), width.
                const float R2D = 57.29577951f;
                ImVec2 ctr = toolUV_[0], s0 = toolUV_[1], endPt = toolUV_[2], wpk = toolUV_[3];
                float rad = std::hypot(s0.x - ctr.x, s0.y - ctr.y);
                if (rad < 1e-4f) { st.addLog("[sketch] Arc Slot: center == start"); resetTool(); }
                else {
                    float a0 = std::atan2(s0.y - ctr.y, s0.x - ctr.x) * R2D;
                    float a1 = std::atan2(endPt.y - ctr.y, endPt.x - ctr.x) * R2D;
                    if (a1 <= a0) a1 += 360.0f;            // enforce CCW a0 -> a1
                    // width = total capsule width = 2 * |dist(wpk, center) - radius|.
                    float w = 2.0f * std::fabs(std::hypot(wpk.x - ctr.x, wpk.y - ctr.y) - rad);
                    if (w < 1e-3f || w >= 2.0f * rad) w = std::fmin(rad * 0.3f, rad * 0.9f);
                    nlohmann::json ent = {{"type", "arc_slot"},
                        {"center", nlohmann::json::array({ctr.x, ctr.y})},
                        {"radius", rad}, {"a0", a0}, {"a1", a1}, {"width", w}};
                    addEntity(st, ent, nlohmann::json::array());
                    resetTool();
                }
            } else if (tool_ == SketchTool::SlotArc3pt && toolUV_.size() == 4) {
                // 3 points on the spine arc (circumcircle), then width.
                const float R2D = 57.29577951f;
                float cu, cv, rad;
                if (!circumcircle(toolUV_[0], toolUV_[1], toolUV_[2], cu, cv, rad)) {
                    st.addLog("[sketch] 3Pt Arc Slot: 3 points are collinear — pick a 3rd off the line");
                    toolUV_.pop_back(); toolSnaps_.pop_back();   // drop the width pick, keep retrying
                } else {
                    ImVec2 p0 = toolUV_[0], pm = toolUV_[1], p2 = toolUV_[2], wpk = toolUV_[3];
                    float a0 = std::atan2(p0.y - cv, p0.x - cu) * R2D;
                    float amid = std::atan2(pm.y - cv, pm.x - cu) * R2D;
                    float a1 = std::atan2(p2.y - cv, p2.x - cu) * R2D;
                    auto norm = [](float a) { while (a < 0) a += 360.0f; while (a >= 360.0f) a -= 360.0f; return a; };
                    float sweep = norm(a1 - a0), midSw = norm(amid - a0);
                    if (midSw > sweep) { float t = a0; a0 = a1; a1 = t; }  // CCW must pass thru mid
                    if (a1 <= a0) a1 += 360.0f;
                    float w = 2.0f * std::fabs(std::hypot(wpk.x - cu, wpk.y - cv) - rad);
                    if (w < 1e-3f || w >= 2.0f * rad) w = std::fmin(rad * 0.3f, rad * 0.9f);
                    nlohmann::json ent = {{"type", "arc_slot"},
                        {"center", nlohmann::json::array({cu, cv})},
                        {"radius", rad}, {"a0", a0}, {"a1", a1}, {"width", w}};
                    addEntity(st, ent, nlohmann::json::array());
                    resetTool();
                }
            } else if (tool_ == SketchTool::EllipsePartial && toolUV_.size() == 5) {
                // center, rx point, ry/rot point, start-angle, end-angle.
                ImVec2 ctr = toolUV_[0], rxp = toolUV_[1], ryp = toolUV_[2];
                float rot = std::atan2(rxp.y - ctr.y, rxp.x - ctr.x);
                float rx = std::hypot(rxp.x - ctr.x, rxp.y - ctr.y);
                float ca = std::cos(rot), sa = std::sin(rot);
                float ry = std::fabs(-(ryp.x - ctr.x) * sa + (ryp.y - ctr.y) * ca);
                if (rx < 1e-4f || ry < 1e-4f) { st.addLog("[sketch] Partial Ellipse: degenerate radii"); resetTool(); }
                else {
                    auto angOf = [&](ImVec2 q) {
                        float lx = (q.x - ctr.x) * ca + (q.y - ctr.y) * sa;
                        float ly = -(q.x - ctr.x) * sa + (q.y - ctr.y) * ca;
                        return std::atan2(ly / ry, lx / rx);
                    };
                    // Contract (doc_schema.md §3.1): rot/a0/a1 are DEGREES, angles in the
                    // un-rotated ellipse frame, CCW a0 -> a1.
                    const float kR2D = 57.29577951f;
                    float a0 = angOf(toolUV_[3]) * kR2D, a1 = angOf(toolUV_[4]) * kR2D;
                    if (a1 <= a0) a1 += 360.0f;   // enforce CCW sweep
                    nlohmann::json ent = {{"type", "ellipse_arc"},
                        {"center", nlohmann::json::array({ctr.x, ctr.y})},
                        {"rx", rx}, {"ry", ry}, {"rot", rot * kR2D}, {"a0", a0}, {"a1", a1}};
                    addEntity(st, ent, nlohmann::json::array());
                    resetTool();
                }
            } else if (tool_ == SketchTool::Parabola && toolUV_.size() == 4) {
                // focus, vertex, start, end picks. Contract (doc_schema.md §3.1) stores the
                // range as t0/t1: signed extents along w = perp(unit(focus - vertex)), i.e.
                // t = (P - vertex)·w in the parabola's own frame.
                ImVec2 focus = toolUV_[0], vertex = toolUV_[1], s0 = toolUV_[2], s1 = toolUV_[3];
                float ux = focus.x - vertex.x, uy = focus.y - vertex.y;
                float ul = std::hypot(ux, uy);
                if (ul < 1e-4f) { st.addLog("[sketch] Parabola: focus == vertex"); resetTool(); }
                else {
                    ux /= ul; uy /= ul;
                    float wx = -uy, wy = ux;   // perp(u)
                    float t0 = (s0.x - vertex.x) * wx + (s0.y - vertex.y) * wy;
                    float t1 = (s1.x - vertex.x) * wx + (s1.y - vertex.y) * wy;
                    nlohmann::json ent = {{"type", "parabola"},
                        {"vertex", nlohmann::json::array({vertex.x, vertex.y})},
                        {"focus", nlohmann::json::array({focus.x, focus.y})},
                        {"t0", t0}, {"t1", t1}};
                    addEntity(st, ent, nlohmann::json::array());
                    resetTool();
                }
            } else if (tool_ == SketchTool::Conic && toolUV_.size() == 4) {
                // p0, p1, apex, then a 4th click sets rho (projection onto mid->apex).
                ImVec2 P0 = toolUV_[0], P1 = toolUV_[1], apex = toolUV_[2], rhoPk = toolUV_[3];
                ImVec2 mid((P0.x + P1.x) * 0.5f, (P0.y + P1.y) * 0.5f);
                float dx = apex.x - mid.x, dy = apex.y - mid.y; float L2 = dx * dx + dy * dy;
                float rho = L2 > 1e-9f ? ((rhoPk.x - mid.x) * dx + (rhoPk.y - mid.y) * dy) / L2 : 0.5f;
                rho = std::fmax(0.01f, std::fmin(0.99f, rho));
                nlohmann::json ent = {{"type", "conic"},
                    {"p0", nlohmann::json::array({P0.x, P0.y})},
                    {"p1", nlohmann::json::array({P1.x, P1.y})},
                    {"apex", nlohmann::json::array({apex.x, apex.y})},
                    {"rho", rho}};
                addEntity(st, ent, nlohmann::json::array());
                resetTool();

            // ---- C42 tool completions ----
            } else if (tool_ == SketchTool::Arc3pt && toolUV_.size() == 3) {
                // p1 = arc start, p2 = arc end, p3 = point ON the arc -> circumcircle; ccw chosen
                // so the sweep from start to end passes through p3.
                float cu, cv, rad;
                if (!circumcircle(toolUV_[0], toolUV_[1], toolUV_[2], cu, cv, rad)) {
                    st.addLog("[sketch] 3-Pt Arc: points are collinear \xE2\x80\x94 pick the on-arc point off the line");
                    toolUV_.pop_back(); toolSnaps_.pop_back();
                } else {
                    const float R2D = 57.29577951f;
                    float aS = std::atan2(toolUV_[0].y - cv, toolUV_[0].x - cu) * R2D;
                    float aE = std::atan2(toolUV_[1].y - cv, toolUV_[1].x - cu) * R2D;
                    float aM = std::atan2(toolUV_[2].y - cv, toolUV_[2].x - cu) * R2D;
                    auto norm = [](float a) { while (a < 0) a += 360.0f; while (a >= 360.0f) a -= 360.0f; return a; };
                    // Always emit a CCW arc (the solver's solved records drop "ccw", so a false
                    // flag would re-render wrong after the first solve): if the CCW sweep p1->p2
                    // misses p3, store the arc as CCW p2->p1 instead (same geometry).
                    bool fwd = norm(aM - aS) <= norm(aE - aS);
                    float a0 = fwd ? aS : aE, a1 = fwd ? aE : aS;
                    // Endpoint snaps -> coincidents onto the arc's start/end handles (like Line);
                    // when reversed, p1 sits on the "end" handle and p2 on "start".
                    // C49: only PLAIN point snaps become coincidents here — midpoint/rim snaps on
                    // arc picks stay positional (coincRef filters the prefixed records).
                    nlohmann::json cs = nlohmann::json::array();
                    std::string s0 = coincRef(toolSnaps_[0]), s1 = coincRef(toolSnaps_[1]);
                    if (!s0.empty())
                        cs.push_back({{"type", "coincident"},
                                      {"refs", {s0, fwd ? "$self.start" : "$self.end"}}});
                    if (!s1.empty())
                        cs.push_back({{"type", "coincident"},
                                      {"refs", {s1, fwd ? "$self.end" : "$self.start"}}});
                    nlohmann::json ent = {{"type", "arc"},
                        {"center", nlohmann::json::array({cu, cv})}, {"radius", rad},
                        {"start", a0}, {"end", a1}, {"ccw", true}};
                    addEntity(st, ent, cs);
                    resetTool();
                }
            } else if (tool_ == SketchTool::Polygon && toolUV_.size() == 2) {
                // center + one vertex -> inscribed N-gon (closed equal-edged line loop).
                ImVec2 ctr = toolUV_[0], vtx = toolUV_[1];
                float R = std::hypot(vtx.x - ctr.x, vtx.y - ctr.y);
                if (R < 1e-4f) {
                    st.addLog("[sketch] Polygon: vertex == center \xE2\x80\x94 pick a vertex away from the center");
                    toolUV_.pop_back(); toolSnaps_.pop_back();
                } else {
                    float a0 = std::atan2(vtx.y - ctr.y, vtx.x - ctr.x);
                    std::vector<ImVec2> corners;
                    for (int i = 0; i < polySides_; ++i) {
                        float a = a0 + 6.2831853f * i / (float)polySides_;
                        corners.push_back(ImVec2(ctr.x + R * std::cos(a), ctr.y + R * std::sin(a)));
                    }
                    auto ids = addPolyLoop(st, corners, /*equalEdges=*/true);
                    // SolidWorks-style inscribed CONSTRUCTION circle: anchors the polygon's center
                    // (the center pick's snap lands on the circle center) and holds every vertex on
                    // it, so a polygon dropped on the origin stays centered + dimensionable.
                    nlohmann::json ccs = nlohmann::json::array();
                    pushSnapConstraint(toolSnaps_[0], "$self.center", ccs);
                    std::string cid = emitConstructionEntity(st,
                        {{"type", "circle"}, {"construction", true},
                         {"center", nlohmann::json::array({ctr.x, ctr.y})}, {"radius", R}}, ccs);
                    if (!cid.empty()) {
                        for (const auto& lid : ids)
                            if (!lid.empty())
                                emitConstructionConstraint(st, {{"type", "point_on"},
                                    {"refs", {lid + ".p1", cid}}});
                        // The picked vertex (corners[0] == ids[0].p1) keeps its own snap too.
                        if (!ids.empty() && !ids[0].empty()) {
                            nlohmann::json vcs = nlohmann::json::array();
                            pushSnapConstraint(toolSnaps_[1], ids[0] + ".p1", vcs);
                            for (const auto& c : vcs) emitConstructionConstraint(st, c);
                        }
                    }
                    resolve(st);
                    resetTool();
                }
            } else if (tool_ == SketchTool::Star && toolUV_.size() == 2) {
                // center + one tip vertex -> 2*polyStarPoints_ alternating outer/inner corners.
                ImVec2 ctr = toolUV_[0], vtx = toolUV_[1];
                float R = std::hypot(vtx.x - ctr.x, vtx.y - ctr.y);
                if (R < 1e-4f) {
                    st.addLog("[sketch] Star: vertex == center \xE2\x80\x94 pick a tip away from the center");
                    toolUV_.pop_back(); toolSnaps_.pop_back();
                } else {
                    float a0 = std::atan2(vtx.y - ctr.y, vtx.x - ctr.x);
                    float Ri = R * polyStarRatio_;
                    int pts = polyStarPoints_;
                    std::vector<ImVec2> corners;
                    corners.reserve(2 * pts);
                    for (int i = 0; i < 2 * pts; ++i) {
                        float a = a0 + (float)i * 3.14159265f / (float)pts;
                        float r = (i & 1) ? Ri : R;
                        corners.push_back(ImVec2(ctr.x + r * std::cos(a), ctr.y + r * std::sin(a)));
                    }
                    addPolyLoop(st, corners, /*equalEdges=*/false);
                    resolve(st);
                    resetTool();
                }
            } else if (tool_ == SketchTool::RectCenter && toolUV_.size() == 2) {
                // center + corner -> axis-aligned rect (same per-edge h/v relations as RectCorner).
                ImVec2 ctr = toolUV_[0], cor = toolUV_[1];
                float dx = std::fabs(cor.x - ctr.x), dy = std::fabs(cor.y - ctr.y);
                if (dx < 1e-4f || dy < 1e-4f) {
                    st.addLog("[sketch] Center Rectangle: zero width/height \xE2\x80\x94 pick a corner off the axes");
                    toolUV_.pop_back(); toolSnaps_.pop_back();
                } else {
                    ImVec2 quad[4] = {ImVec2(ctr.x - dx, ctr.y - dy), ImVec2(ctr.x + dx, ctr.y - dy),
                                      ImVec2(ctr.x + dx, ctr.y + dy), ImVec2(ctr.x - dx, ctr.y + dy)};
                    const char* rel[4] = {"horizontal", "vertical", "horizontal", "vertical"};
                    auto ids = addQuadLoop(st, quad, rel);
                    // SolidWorks-style center rectangle: a construction diagonal (corner 0 -> corner
                    // 2) plus a center CONSTRUCTION point pinned to the diagonal's midpoint. The
                    // center pick's snap lands on that point, so the rect stays centered on it.
                    if (ids.size() == 4 && !ids[0].empty() && !ids[2].empty()) {
                        nlohmann::json pcs = nlohmann::json::array();
                        pushSnapConstraint(toolSnaps_[0], "$self.p", pcs);
                        std::string pc = emitConstructionEntity(st,
                            {{"type", "point"}, {"construction", true},
                             {"p", nlohmann::json::array({ctr.x, ctr.y})}}, pcs);
                        std::string diag = emitConstructionEntity(st,
                            {{"type", "line"}, {"construction", true},
                             {"p1", nlohmann::json::array({quad[0].x, quad[0].y})},
                             {"p2", nlohmann::json::array({quad[2].x, quad[2].y})}},
                            nlohmann::json::array());
                        if (!pc.empty() && !diag.empty()) {
                            emitConstructionConstraint(st, {{"type", "coincident"},
                                {"refs", {diag + ".p1", ids[0] + ".p1"}}});
                            emitConstructionConstraint(st, {{"type", "coincident"},
                                {"refs", {diag + ".p2", ids[2] + ".p1"}}});
                            emitConstructionConstraint(st, {{"type", "midpoint"},
                                {"refs", {pc + ".p", diag}}});
                        }
                        resolve(st);
                    }
                    resetTool();
                }
            } else if (tool_ == SketchTool::Point && toolUV_.size() == 1) {
                nlohmann::json ent = {{"type", "point"},
                    {"p", nlohmann::json::array({toolUV_[0].x, toolUV_[0].y})}};
                // C49: a snapped Point pick gets the matching relation (its handle is "$self.p").
                nlohmann::json cs = nlohmann::json::array();
                const std::string& sp = toolSnaps_[0];
                if (snapHasPrefix(sp, "mid:"))
                    cs.push_back({{"type", "midpoint"}, {"refs", {"$self.p", sp.substr(4)}}});
                else if (snapHasPrefix(sp, "rim:") || snapHasPrefix(sp, "tan:"))
                    cs.push_back({{"type", "point_on"}, {"refs", {"$self.p", sp.substr(4)}}});
                else if (!sp.empty())
                    cs.push_back({{"type", "coincident"}, {"refs", {sp, "$self.p"}}});
                addEntity(st, ent, cs);
                toolUV_.clear(); toolSnaps_.clear();   // stay armed (SW point tool repeats)
            } else if (tool_ == SketchTool::Ellipse && toolUV_.size() == 3) {
                // center, major-axis end (rx + rot), minor point (ry = perp distance).
                ImVec2 ctr = toolUV_[0], rxp = toolUV_[1], ryp = toolUV_[2];
                float rot = std::atan2(rxp.y - ctr.y, rxp.x - ctr.x);
                float rx = std::hypot(rxp.x - ctr.x, rxp.y - ctr.y);
                float ca = std::cos(rot), sa = std::sin(rot);
                float ry = std::fabs(-(ryp.x - ctr.x) * sa + (ryp.y - ctr.y) * ca);
                if (rx < 1e-4f || ry < 1e-4f) {
                    st.addLog("[sketch] Ellipse: degenerate radii \xE2\x80\x94 pick the minor point off the major axis");
                    toolUV_.pop_back(); toolSnaps_.pop_back();
                } else {
                    const float kR2D = 57.29577951f;
                    nlohmann::json ent = {{"type", "ellipse"},
                        {"center", nlohmann::json::array({ctr.x, ctr.y})},
                        {"rx", rx}, {"ry", ry}, {"rot", rot * kR2D}};   // rot in degrees (schema)
                    // Persist the center pick's snap (coincident / alignment) onto $self.center.
                    nlohmann::json cs = nlohmann::json::array();
                    pushSnapConstraint(toolSnaps_[0], "$self.center", cs);
                    addEntity(st, ent, cs);
                    resetTool();
                }
            }
        }

        // C42: Polygon side count while the tool is armed.
        //   • number keys 3-9 (top row or keypad) quick-set 3..9 sides
        //   • +/= or ]  add a side, -/_ or [  remove one (clamped 3..64) -> octagons, decagons,
        //     dodecagons and beyond, not just the default hexagon.
        if (tool_ == SketchTool::Polygon && !io.WantTextInput) {
            for (int d = 3; d <= 9; ++d) {
                if (ImGui::IsKeyPressed((ImGuiKey)(ImGuiKey_0 + d)) ||
                    ImGui::IsKeyPressed((ImGuiKey)(ImGuiKey_Keypad0 + d)))
                    polySides_ = d;
            }
            const bool inc = ImGui::IsKeyPressed(ImGuiKey_Equal)        // '=' (shift gives '+')
                          || ImGui::IsKeyPressed(ImGuiKey_KeypadAdd)
                          || ImGui::IsKeyPressed(ImGuiKey_RightBracket);
            const bool dec = ImGui::IsKeyPressed(ImGuiKey_Minus)
                          || ImGui::IsKeyPressed(ImGuiKey_KeypadSubtract)
                          || ImGui::IsKeyPressed(ImGuiKey_LeftBracket);
            if (inc && polySides_ < 64) ++polySides_;
            if (dec && polySides_ > 3)  --polySides_;
        }
        // Star tip count: +/-/[/] adjust polyStarPoints_ (clamped 3..12).
        if (tool_ == SketchTool::Star && !io.WantTextInput) {
            const bool inc = ImGui::IsKeyPressed(ImGuiKey_Equal)
                          || ImGui::IsKeyPressed(ImGuiKey_KeypadAdd)
                          || ImGui::IsKeyPressed(ImGuiKey_RightBracket);
            const bool dec = ImGui::IsKeyPressed(ImGuiKey_Minus)
                          || ImGui::IsKeyPressed(ImGuiKey_KeypadSubtract)
                          || ImGui::IsKeyPressed(ImGuiKey_LeftBracket);
            if (inc && polyStarPoints_ < 12) ++polyStarPoints_;
            if (dec && polyStarPoints_ > 3)  --polyStarPoints_;
        }

        // C42: Spline finishing — Enter / right-click / double-click commits (>= 2 points); a
        // right-click with fewer just drops the in-progress points (mirrors the Line-chain RMB).
        // Style Spline shares the exact same collect/finish UX (>= 2 poles) and only differs in the
        // emitted entity type ("style_spline" — control poles, not interpolated through-points).
        if (tool_ == SketchTool::Spline || tool_ == SketchTool::StyleSpline) {
            bool finish = false;
            if (imgHovered && ImGui::IsMouseDoubleClicked(ImGuiMouseButton_Left) &&
                toolUV_.size() >= 3) {
                // The double-click's 2nd click pushed a duplicate point this frame — drop it.
                toolUV_.pop_back(); toolSnaps_.pop_back();
                finish = true;
            }
            if (!io.WantTextInput && toolUV_.size() >= 2 &&
                (ImGui::IsKeyPressed(ImGuiKey_Enter) || ImGui::IsKeyPressed(ImGuiKey_KeypadEnter)))
                finish = true;
            if (imgHovered && ImGui::IsMouseClicked(ImGuiMouseButton_Right)) {
                if (toolUV_.size() >= 2) finish = true;
                else { toolUV_.clear(); toolSnaps_.clear(); }
            }
            if (finish && toolUV_.size() >= 2) {
                nlohmann::json pts = nlohmann::json::array();
                for (const auto& q : toolUV_) pts.push_back(nlohmann::json::array({q.x, q.y}));
                const char* entType = (tool_ == SketchTool::StyleSpline) ? "style_spline" : "spline";
                nlohmann::json ent = {{"type", entType}, {"points", pts}};
                // Persist each through-point/pole pick's snap onto $self.p<i> (endpoints landing on
                // a vertex are the usual case; alignment guides on any point carry too).
                nlohmann::json cs = nlohmann::json::array();
                for (size_t i = 0; i < toolSnaps_.size() && i < toolUV_.size(); ++i)
                    pushSnapConstraint(toolSnaps_[i], "$self.p" + std::to_string(i), cs);
                addEntity(st, ent, cs);
                resetTool();
            }
        }
        // C37-D: right-click ends the current chain/segment but keeps the tool armed (SW muscle
        // memory: the Line tool drops its in-progress points and waits for a fresh first point).
        // mouse_gestures is disabled in-sketch (viewport_panel), so the RMB is free here; consume it.
        // C42: Spline / Style Spline own their RMB (finish/drop, handled above).
        if (tool_ != SketchTool::Spline && tool_ != SketchTool::StyleSpline && imgHovered &&
            ImGui::IsMouseClicked(ImGuiMouseButton_Right)) {
            toolUV_.clear(); toolSnaps_.clear(); chainPrevId_.clear();
        }
        // ESC ends the tool.
        if (ImGui::IsKeyPressed(ImGuiKey_Escape)) cancelTool();
    }

    // ---- C37-D: SolidWorks-grade Smart Dimension state machine ----
    if (tool_ == SketchTool::SmartDim) {
        st.requestCursor(ui::sketch::CursorKind::Crosshair);
        const ImU32 hiCol = col(pal.accent);
        const ImU32 pickCol = col(pal.accent, 0.95f);
        // Highlight already-picked targets. C38 (S1): resolve the picked entity by id (ents_ may have
        // been refilled since the pick) — skip the highlight when it's gone.
        for (const auto& p : dimPicks_) {
            if (p.isPoint) { dl->AddCircle(uvToScreen(p.u, p.v), 6.0f, pickCol, 12, 2.0f); continue; }
            const SketchEntity* ep = entById(p.entId);
            if (ep) {
                const SketchEntity& e = *ep;
                if (e.type == "line")
                    dl->AddLine(uvToScreen(e.p1[0], e.p1[1]), uvToScreen(e.p2[0], e.p2[1]), pickCol, 3.0f);
                else if (e.type == "circle" || e.type == "arc")
                    dl->AddCircle(uvToScreen(e.center[0], e.center[1]),
                                  std::hypot(uvToScreen(e.center[0] + e.radius, e.center[1]).x -
                                             uvToScreen(e.center[0], e.center[1]).x, 0.0f), pickCol, 48, 3.0f);
            }
        }
        // A pick can still be chained while fewer than 2 targets are collected — even when a
        // single-pick combo (diameter / line length) is already previewing: clicking ANOTHER
        // target replaces it with the two-pick combo (SolidWorks behavior; this also makes the
        // line-line angle and all circle-distance combos actually reachable).
        const bool canAddPick = !dimPopupOpen_ && !overDefOpen_ && dimPicks_.size() < 2;
        DimPick hoverPick; bool hoverHit = false;
        if (canAddPick && imgHovered) hoverHit = dimHitTest(io.MousePos, hoverPick);
        if (hoverHit) {
            // A midpoint pick (handle ".mid") draws as a small filled diamond — the SolidWorks
            // midpoint glyph — to distinguish it from an endpoint circle.
            bool isMid = hoverPick.isPoint && hoverPick.handle.size() > 4 &&
                         hoverPick.handle.compare(hoverPick.handle.size() - 4, 4, ".mid") == 0;
            if (isMid) {
                ImVec2 m = uvToScreen(hoverPick.u, hoverPick.v); float r = 5.0f;
                dl->AddQuadFilled(ImVec2(m.x, m.y - r), ImVec2(m.x + r, m.y),
                                  ImVec2(m.x, m.y + r), ImVec2(m.x - r, m.y), hiCol);
            } else if (hoverPick.isPoint) {
                dl->AddCircle(uvToScreen(hoverPick.u, hoverPick.v), 6.0f, hiCol, 12, 2.0f);
            } else if (hoverPick.entIdx >= 0) {
                const SketchEntity& e = ents_[hoverPick.entIdx];
                if (e.type == "line") {
                    dl->AddLine(uvToScreen(e.p1[0], e.p1[1]), uvToScreen(e.p2[0], e.p2[1]), hiCol, 2.5f);
                    // SolidWorks shows the midpoint marker whenever a line is hovered, so the user
                    // knows they can snap to it — a hollow diamond at the center.
                    ImVec2 m = uvToScreen((e.p1[0]+e.p2[0])*0.5f, (e.p1[1]+e.p2[1])*0.5f); float r = 5.0f;
                    dl->AddQuad(ImVec2(m.x, m.y - r), ImVec2(m.x + r, m.y),
                                ImVec2(m.x, m.y + r), ImVec2(m.x - r, m.y), hiCol, 1.6f);
                } else if (e.type == "circle" || e.type == "arc")
                    dl->AddCircle(uvToScreen(e.center[0], e.center[1]),
                                  std::hypot(uvToScreen(e.center[0] + e.radius, e.center[1]).x -
                                             uvToScreen(e.center[0], e.center[1]).x, 0.0f), hiCol, 48, 2.5f);
            }
        }

        // External (model) geometry: when nothing sketch-side is under the cursor, probe the
        // id-buffer for a model edge/face. st.hover drives the standard 3D hover highlight
        // (rendered next frame); a click projects the reference into the sketch as a FIXED
        // construction entity (worker sketch_project_ref) and chains it as a pick.
        worker::PickHit extHit{};
        if (canAddPick && imgHovered && !hoverHit && st.hasDocMesh && st.idPass) {
            int px = (int)(io.MousePos.x - imgMin.x), py = (int)(io.MousePos.y - imgMin.y);
            worker::PickHit h = toWorkerPick(st.idPass->pickFinal(px, py, vw, vh));
            if (h.valid() && (h.kind == worker::PickKind::Edge || h.kind == worker::PickKind::Face))
                extHit = h;
        }
        st.hover = extHit;   // empty hit = no highlight; cleared when SmartDim disarms (below)

        // Phase A: collecting picks (no full combo yet). A click adds the hovered target.
        if (!dimPopupOpen_ && !overDefOpen_) {
            // Rebuild the pending dim from the current picks + live cursor (drives the preview AND
            // the live h/v/aligned variant choice for point-point).
            dimBuildPending(io.MousePos, mu, mv);

            bool clicked = imgHovered && ImGui::IsMouseClicked(ImGuiMouseButton_Left);
            bool pickAdded = false;
            if (clicked && canAddPick && dimHoverCid_.empty()) {
                // C47: dimHoverCid_ guard — a click on an existing dim's text belongs to the
                // drag / double-click-edit handler above, not a new pick.
                if (hoverHit) {
                    // Avoid duplicate picks of the same target. C38 (S1): compare entity picks by
                    // id (stable) rather than ents_ index (per-frame, may have shifted).
                    bool dup = false;
                    for (const auto& q : dimPicks_)
                        if (q.isPoint == hoverPick.isPoint &&
                            (hoverPick.isPoint ? q.handle == hoverPick.handle
                                               : q.entId == hoverPick.entId)) dup = true;
                    if (!dup) {
                        // Shift+click a circle/arc = tangent (near-rim "min") anchor; plain click
                        // anchors at the center. Either way the Modify box can switch it after.
                        if (!hoverPick.isPoint) {
                            const SketchEntity* ce = entById(hoverPick.entId);
                            if (ce && (ce->type == "circle" || ce->type == "arc"))
                                hoverPick.cond = io.KeyShift ? "min" : "center";
                        }
                        dimPicks_.push_back(hoverPick);
                        pickAdded = true;
                        dimBuildPending(io.MousePos, mu, mv);   // refresh the combo immediately
                    }
                } else if (extHit.valid()) {
                    std::string perr;
                    std::string selStr = st.docMirror().resolvePick(
                        st.worker(), extHit.body_id,
                        extHit.kind == worker::PickKind::Edge ? "edge" : "face",
                        extHit.topo_id, perr);
                    nlohmann::json selJs;
                    if (!selStr.empty()) { try { selJs = nlohmann::json::parse(selStr); } catch (...) {} }
                    if (selJs.is_object()) {
                        nlohmann::json ref = {
                            {"kind", extHit.kind == worker::PickKind::Edge ? "edge" : "face"},
                            {"selector", selJs}};
                        if (!addProjectedDimRef(st, ref, selStr).empty()) {
                            pickAdded = true;
                            dimBuildPending(io.MousePos, mu, mv);
                        }
                    } else {
                        st.toast(ui::sketch::ToastLevel::Warn, "Can't reference that geometry" +
                                 (perr.empty() ? std::string() : ": " + perr));
                    }
                }
            }
            if (dimHasPending_) {
                // LIVE PREVIEW: a full annotation following the cursor until the placement click.
                SketchConstraint pc;
                pc.type = dimType_; pc.axis = dimAxis_;
                pc.hasMeasured = true; pc.measured = dimMeasured_;
                if (dimPending_.contains("refs"))
                    for (const auto& rr : dimPending_["refs"]) pc.refs.push_back(rr.get<std::string>());
                if (dimPending_.contains("cond"))
                    for (const auto& q : dimPending_["cond"]) pc.cond.push_back(q.get<std::string>());
                drawDimension(dl, pc, hiCol, hiCol, true, mu, mv);
                // Placement click (a click that didn't chain a pick) stores the placement and
                // opens the Modify box.
                if (clicked && !pickAdded) {
                    dimPlaceU_ = mu; dimPlaceV_ = mv;
                    dimOpenModify(io.MousePos);
                }
            }
        }

        // Esc semantics: with the Modify box open, item-4 handler closes it (below). With picks in
        // the buffer, first Esc clears the buffer; second Esc exits the tool.
        if (!dimPopupOpen_ && ImGui::IsKeyPressed(ImGuiKey_Escape)) {
            escHandled = true;   // C41 (T1): pick-buffer clear keeps the tool armed
            if (!dimPicks_.empty()) { dimPicks_.clear(); dimHasPending_ = false; dimPending_ = nullptr; }
            else cancelTool();
        }
        // Right-click also clears the pick buffer (consume it; gestures off in-sketch).
        if (imgHovered && ImGui::IsMouseClicked(ImGuiMouseButton_Right)) {
            dimPicks_.clear(); dimHasPending_ = false; dimPending_ = nullptr;
        }
    } else if (st.hover.valid()) {
        // Smart Dimension was the only in-sketch writer of st.hover (model-ref picking); clear a
        // stale highlight once the tool disarms (the viewport's own picking is off in-sketch).
        st.hover = worker::PickHit{};
    }

    // ---- Wave 4C/4D: paste-placement preview (drawn LAST, on top of the existing geometry) ----
    // imgHovered was forced false above so no tool/selection interaction consumed the placement
    // click. The follow-cursor outline preview is shifted by (cursor - copyOrigin); a left-click over
    // the canvas commits the paste at that offset, Esc cancels. We re-derive canvas-hover from the
    // image rect (the suppressed imgHovered can't be reused for the commit click).
    if (pastePlacing_) {
        st.requestCursor(ui::sketch::CursorKind::Crosshair);
        drawPastePreview(st, dl, io.MousePos);
        const char* banner = "Click to place \xE2\x80\x94 Esc to cancel";
        ImVec2 bts = ImGui::CalcTextSize(banner);
        float bcx = (imgMin.x + imgMax.x) * 0.5f;
        ImVec2 b0(bcx - bts.x * 0.5f - 14, imgMin.y + 52.0f * ui::sketch::uiScale());
        ImVec2 b1(bcx + bts.x * 0.5f + 14, b0.y + bts.y + 12);
        dl->AddRectFilled(b0, b1, col(pal.overlayBg), 6.0f);
        dl->AddRect(b0, b1, col(pal.overlayBorder), 6.0f);
        dl->AddText(ImVec2(b0.x + 14, b0.y + 6), col(pal.overlayText), banner);
        bool overCanvas = ImGui::IsMouseHoveringRect(imgMin, imgMax);
        // Exclude the ✓/✗ confirmation corner (top-right) so a click there doesn't also commit the
        // paste — same geometry the top-of-function corner block uses to steal hover from tools.
        {
            const float sz = 30.0f, pad = 12.0f;
            ImVec2 okMin(imgMax.x - pad - sz, imgMin.y + pad), okMax(okMin.x + sz, okMin.y + sz);
            ImVec2 cxMin(okMin.x, okMax.y + 6), cxMax(cxMin.x + sz, cxMin.y + sz);
            if (ImGui::IsMouseHoveringRect(okMin, okMax) || ImGui::IsMouseHoveringRect(cxMin, cxMax))
                overCanvas = false;
        }
        if (ImGui::IsKeyPressed(ImGuiKey_Escape)) {
            pastePlacing_ = false; pasteBundle_ = nlohmann::json();
            st.toast(ui::sketch::ToastLevel::Info, "Paste cancelled");
        } else if (overCanvas && ImGui::IsMouseClicked(ImGuiMouseButton_Left)) {
            float cu = 0, cv = 0; screenToUV(io.MousePos, cu, cv);
            commitPasteEntities(st, cu - pasteAnchorUV_.x, cv - pasteAnchorUV_.y);
        }
    }

    dl->PopClipRect();

    // ---- C37-D: SolidWorks confirmation corner (top-right): ✓ exit & keep, ✗ cancel & revert ----
    {
        ImDrawList* wdl = ImGui::GetWindowDrawList();
        const float sz = 30.0f, pad = 12.0f;
        // Two stacked square buttons in the very top-right corner.
        ImVec2 okMin(imgMax.x - pad - sz, imgMin.y + pad);
        ImVec2 okMax(okMin.x + sz, okMin.y + sz);
        ImVec2 cxMin(okMin.x, okMax.y + 6);
        ImVec2 cxMax(cxMin.x + sz, cxMin.y + sz);
        bool okOver = ImGui::IsMouseHoveringRect(okMin, okMax);
        bool cxOver = ImGui::IsMouseHoveringRect(cxMin, cxMax);
        // ✓ keep
        wdl->AddRectFilled(okMin, okMax, col(pal.statusOk, okOver ? 0.95f : 0.80f), 5.0f);
        {
            ImVec2 c(okMin.x + sz * 0.5f, okMin.y + sz * 0.5f);
            wdl->AddLine(ImVec2(c.x - 7, c.y + 0), ImVec2(c.x - 2, c.y + 6), col(pal.windowBg), 2.6f);
            wdl->AddLine(ImVec2(c.x - 2, c.y + 6), ImVec2(c.x + 8, c.y - 6), col(pal.windowBg), 2.6f);
        }
        // ✗ cancel
        wdl->AddRectFilled(cxMin, cxMax, col(pal.statusErr, cxOver ? 0.95f : 0.80f), 5.0f);
        {
            ImVec2 c(cxMin.x + sz * 0.5f, cxMin.y + sz * 0.5f);
            wdl->AddLine(ImVec2(c.x - 6, c.y - 6), ImVec2(c.x + 6, c.y + 6), col(pal.windowBg), 2.6f);
            wdl->AddLine(ImVec2(c.x - 6, c.y + 6), ImVec2(c.x + 6, c.y - 6), col(pal.windowBg), 2.6f);
        }
        if (ImGui::IsMouseClicked(ImGuiMouseButton_Left) && (okOver || cxOver)) {
            // C38 (S8): if the Smart Dimension Modify box is open when ✓/✗ is clicked, the pending
            // dimension is INTENTIONALLY DISCARDED (the user chose to leave the sketch instead of
            // finishing the dim). Close the box and clear the pick buffer explicitly FIRST so
            // resetTool/exit teardown can't leave half-state behind, then run exit/cancel.
            if (dimPopupOpen_) {
                dimPopupOpen_ = false; dimError_.clear(); dimEditCid_.clear();
                dimPicks_.clear(); dimHasPending_ = false; dimPending_ = nullptr;
                dimAxis_.clear(); dimPopupDriven_ = false;
            }
            if (okOver)      exitSketch(st, /*keep=*/true);
            else if (cxOver) cancelSketch(st);
        }
        if (!active_) return;   // confirmation corner closed the sketch this frame (clip already popped)
    }

    // ---- tool hint chip (top-center) ----
    if (tool_ != SketchTool::None) {
        const char* tn = tool_ == SketchTool::Line ? "Line" :
                         tool_ == SketchTool::RectCorner ? "Rectangle" :
                         tool_ == SketchTool::Circle ? "Circle" :
                         tool_ == SketchTool::ArcCenter ? "Center Arc" :
                         tool_ == SketchTool::Slot ? "Slot" :
                         tool_ == SketchTool::SmartDim ? "Smart Dimension" :
                         tool_ == SketchTool::Rect3ptCorner ? "3 Point Corner Rectangle" :
                         tool_ == SketchTool::Rect3ptCenter ? "3 Point Center Rectangle" :
                         tool_ == SketchTool::Parallelogram ? "Parallelogram" :
                         tool_ == SketchTool::CirclePerimeter ? "Perimeter Circle" :
                         tool_ == SketchTool::SlotCtrStr ? "Centerpoint Slot" :
                         tool_ == SketchTool::SlotArcCtr ? "Centerpoint Arc Slot" :
                         tool_ == SketchTool::SlotArc3pt ? "3 Point Arc Slot" :
                         tool_ == SketchTool::EllipsePartial ? "Partial Ellipse" :
                         tool_ == SketchTool::Parabola ? "Parabola" :
                         tool_ == SketchTool::Conic ? "Conic" :
                         tool_ == SketchTool::Trim ? "Trim" :
                         tool_ == SketchTool::Extend ? "Extend" :
                         tool_ == SketchTool::Offset ? "Offset" :
                         tool_ == SketchTool::SkFillet ? "Sketch Fillet" :
                         tool_ == SketchTool::SkChamfer ? "Sketch Chamfer" :
                         tool_ == SketchTool::Mirror ? "Mirror Entities" :
                         tool_ == SketchTool::LinPattern ? "Linear Pattern" :
                         tool_ == SketchTool::CircPattern ? "Circular Pattern" :
                         tool_ == SketchTool::Convert ? "Convert Entities" :
                         tool_ == SketchTool::Move ? "Move Entities" :
                         tool_ == SketchTool::ArcTangent ? "Tangent Arc" :
                         tool_ == SketchTool::Arc3pt ? "3 Point Arc" :
                         tool_ == SketchTool::Polygon ? "Polygon" :
                         tool_ == SketchTool::RectCenter ? "Center Rectangle" :
                         tool_ == SketchTool::Point ? "Point" :
                         tool_ == SketchTool::Ellipse ? "Ellipse" :
                         tool_ == SketchTool::Spline ? "Spline" :
                         tool_ == SketchTool::StyleSpline ? "Style Spline" :
                         tool_ == SketchTool::Text ? "Text" :
                         tool_ == SketchTool::EqCurve ? "Equation Driven Curve" :
                         tool_ == SketchTool::Star ? "Star" : "";
        // Per-stage step prompt (status-bar style) for the multi-click variants.
        const size_t n = toolUV_.size();
        const char* step = nullptr;
        switch (tool_) {
            case SketchTool::Line:   // C37-D: SW muscle-memory hint
                step = n == 0 ? "click start point" : "click next point \xC2\xB7 right-click to end chain"; break;
            case SketchTool::RectCorner:
                step = n == 0 ? "click first corner" : "click opposite corner"; break;
            case SketchTool::Circle:
                step = n == 0 ? "click center" : "click radius"; break;
            case SketchTool::ArcCenter:
                step = n == 0 ? "click center" : n == 1 ? "click start" : "click end"; break;
            case SketchTool::Slot:
                step = n == 0 ? "click start" : n == 1 ? "click end" : "click width"; break;
            case SketchTool::SmartDim:
                step = dimPicks_.empty()
                     ? "click an entity, point, model edge/face, or a tree plane (Shift+circle = tangent)"
                     : dimPicks_.size() < 2
                     ? "click another entity/point/model ref, or click empty space to place"
                     : "click to place the dimension"; break;
            case SketchTool::Rect3ptCorner:
                step = n == 0 ? "click corner" : n == 1 ? "click edge end" : "click width"; break;
            case SketchTool::Rect3ptCenter:
                step = n == 0 ? "click center" : n == 1 ? "click edge midpoint" : "click width"; break;
            case SketchTool::Parallelogram:
                step = n == 0 ? "click corner 1" : n == 1 ? "click corner 2" : "click corner 3"; break;
            case SketchTool::CirclePerimeter:
                step = n < 3 ? "click 3 points on the circumference" : nullptr; break;
            case SketchTool::SlotCtrStr:
                step = n == 0 ? "click center" : n == 1 ? "click end (length/dir)" : "click width"; break;
            case SketchTool::SlotArcCtr:
                step = n == 0 ? "click arc center" : n == 1 ? "click spine start" :
                       n == 2 ? "click spine end" : "click width"; break;
            case SketchTool::SlotArc3pt:
                step = n < 3 ? "click 3 points on the arc spine" : "click width"; break;
            case SketchTool::EllipsePartial:
                step = n == 0 ? "click center" : n == 1 ? "click major (rx/rot)" :
                       n == 2 ? "click minor (ry)" : n == 3 ? "click start angle" : "click end angle"; break;
            case SketchTool::Parabola:
                step = n == 0 ? "click focus" : n == 1 ? "click vertex" :
                       n == 2 ? "click start" : "click end"; break;
            case SketchTool::Conic:
                step = n == 0 ? "click endpoint 1" : n == 1 ? "click endpoint 2" :
                       n == 2 ? "click apex" : "click to set rho (fullness)"; break;
            case SketchTool::Trim:
                step = "hover a segment (red), click to trim"; break;
            case SketchTool::Extend:
                step = "hover near an end, click to extend"; break;
            case SketchTool::Offset:
                step = offsetStage_ == 0 ? "click entities for the chain, then Enter"
                     : offsetStage_ == 1 ? "type the offset distance, Enter"
                     : "click the side to offset toward"; break;
            case SketchTool::SkFillet: {
                static char fb[64];
                std::snprintf(fb, sizeof(fb), corner1Id_.empty()
                                  ? "pick two lines (radius %.3g mm)"
                                  : "pick the second line (radius %.3g mm)", skFilletRadius_);
                step = fb; break;
            }
            case SketchTool::SkChamfer: {
                static char cb[64];
                std::snprintf(cb, sizeof(cb), corner1Id_.empty()
                                  ? "pick two lines (distance %.3g mm)"
                                  : "pick the second line (distance %.3g mm)", skChamferDist_);
                step = cb; break;
            }
            case SketchTool::Mirror:
                step = mpStage_ == 0 ? "click entities to mirror, then Enter"
                                     : "click the mirror line (construction line)"; break;
            case SketchTool::LinPattern:
                step = mpStage_ == 0 ? "click entities to pattern, then Enter"
                                     : "set count / spacing / direction, then Apply"; break;
            case SketchTool::CircPattern:
                step = mpStage_ == 0 ? "click entities to pattern, then Enter"
                     : mpStage_ == 1 ? "click the pattern center point"
                                     : "set count / angle, then Apply"; break;
            case SketchTool::Convert:
                step = "click a model edge or face to project into the sketch"; break;
            case SketchTool::Move:
                step = mpStage_ == 0 ? "click entities to move, then Enter"
                     : mpStage_ == 1 ? "click the from point (anchor)"
                                     : "click the to point \xC2\xB7 hold Ctrl to copy"; break;
            case SketchTool::ArcTangent:
                step = "click the arc end (it starts tangent to the prior/selected entity)"; break;
            case SketchTool::Arc3pt:
                step = n == 0 ? "click arc start" : n == 1 ? "click arc end"
                     : "click a point on the arc"; break;
            case SketchTool::Polygon:
                step = n == 0 ? "click center" : "click a vertex"; break;
            case SketchTool::RectCenter:
                step = n == 0 ? "click center" : "click a corner"; break;
            case SketchTool::Point:
                step = "click to place a point"; break;
            case SketchTool::Ellipse:
                step = n == 0 ? "click center" : n == 1 ? "click major-axis end" : "click minor (ry)"; break;
            case SketchTool::Spline:
                step = n < 2 ? "click points; Enter / right-click / double-click to finish"
                             : "click more points \xC2\xB7 Enter / right-click / double-click finishes"; break;
            case SketchTool::StyleSpline:
                step = n < 2 ? "click control poles; Enter / right-click / double-click to finish"
                             : "click more poles \xC2\xB7 Enter / right-click / double-click finishes"; break;
            case SketchTool::Text:
                step = textPopupOpen_ ? "type the text + height, then OK"
                                      : "click to place the text"; break;
            case SketchTool::EqCurve:
                step = "enter x(t) / y(t) and the t range, then OK"; break;
            case SketchTool::Airfoil:
                step = "set the NACA code / .dat + chord, then OK"; break;
            case SketchTool::Star:
                step = n == 0 ? "click center" : "click a tip vertex"; break;
            default: break;
        }
        std::string hint = std::string(tn) + (step ? std::string(" \xE2\x80\x94 ") + step : std::string());
        if (tool_ == SketchTool::Polygon) {   // C42: live side count + how to change it
            char sb[80];
            std::snprintf(sb, sizeof(sb), "  [Sides: %d \xC2\xB7 keys 3-9 or +/- to adjust]", polySides_);
            hint += sb;
        }
        if (tool_ == SketchTool::Star) {
            char sb[80];
            std::snprintf(sb, sizeof(sb), "  [Tips: %d \xC2\xB7 +/- to adjust, ratio %.2f]",
                          polyStarPoints_, polyStarRatio_);
            hint += sb;
        }
        hint += "  (ESC to end)";
        ImVec2 ts = ImGui::CalcTextSize(hint.c_str());
        float cx = (imgMin.x + imgMax.x) * 0.5f;
        ImVec2 p0(cx - ts.x * 0.5f - 12, imgMin.y + 12);
        ImVec2 p1(cx + ts.x * 0.5f + 12, p0.y + ts.y + 10);
        ImDrawList* wdl = ImGui::GetWindowDrawList();
        wdl->AddRectFilled(p0, p1, col(pal.overlayBg), 6.0f);
        wdl->AddRect(p0, p1, col(pal.overlayBorder), 6.0f);
        wdl->AddText(ImVec2(p0.x + 12, p0.y + 5), col(pal.overlayText), hint.c_str());
    }

    // ---- status chip (top-LEFT now; confirmation corner owns the top-right) ----
    {
        std::string sl = statusLine();
        // C41 (T4): neutral pill while the sketch is empty (statusLine shows the draw hint).
        ImU32 sc = ents_.empty()  ? col(pal.overlayText, 0.9f)
                 : status_ == "over" ? col(pal.statusErr)
                 : status_ == "full" ? col(pal.statusOk) : col(pal.sketchUnder);
        ImVec2 ts = ImGui::CalcTextSize(sl.c_str());
        ImVec2 p0(imgMin.x + 12, imgMin.y + 12);
        ImVec2 p1b(p0.x + ts.x + 20, p0.y + ts.y + 10);
        ImDrawList* wdl = ImGui::GetWindowDrawList();
        wdl->AddRectFilled(p0, p1b, col(pal.overlayBg), 6.0f);
        wdl->AddRect(p0, p1b, sc, 6.0f, 0, 1.5f);
        wdl->AddText(ImVec2(p0.x + 10, p0.y + 5), sc, sl.c_str());

        // C50f: conflict-freeze warning bar. When the last solve came back solve_rejected:true the
        // engine froze geometry at the last valid coordinates (no least-squares mangling); tell the
        // user the sketch couldn't be solved and point at the red (conflicting) relations to edit.
        // Sits just below the status pill, error-tinted. Cleared automatically when a later solve
        // succeeds (applyEnterResult sets solveRejected_ = false).
        if (solveRejected_) {
            const char* warn = "Sketch could not be solved \xE2\x80\x94 geometry frozen at last valid "
                               "state. Delete or edit a conflicting (red) relation.";
            ImVec2 wts = ImGui::CalcTextSize(warn);
            ImVec2 w0(imgMin.x + 12, p1b.y + 8);
            ImVec2 w1(w0.x + wts.x + 20, w0.y + wts.y + 10);
            ImU32 ec = col(pal.statusErr);
            wdl->AddRectFilled(w0, w1, col(pal.overlayBg), 6.0f);
            wdl->AddRect(w0, w1, ec, 6.0f, 0, 1.5f);
            wdl->AddText(ImVec2(w0.x + 10, w0.y + 5), ec, warn);
        }
    }

    // ---- Smart Dimension Modify box (C50 redesign) ----
    // SolidWorks-style compact panel: titled header with the dim type + live readout, a prominent
    // value field flanked by steppers with the unit suffix, Driven + Arc-condition controls, an
    // inline error line, and a primary OK / Cancel footer. Auto-focused + text pre-selected; Enter
    // commits, Esc closes without committing (pick buffer cleared) but stays in the tool. Expression
    // input ("=...") and full unit math ("100mm+2in/0.5m") keep working.
    if (dimPopupOpen_) {
        const float sc = ui::sketch::uiScale();
        ImGui::SetNextWindowPos(dimPopupPos_, ImGuiCond_Appearing);
        ImGui::SetNextWindowBgAlpha(0.99f);
        ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(12 * sc, 10 * sc));
        ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing, ImVec2(7 * sc, 7 * sc));
        ImGui::PushStyleVar(ImGuiStyleVar_FrameRounding, 4.0f);
        if (ImGui::Begin("##SketchDim", nullptr,
                         ImGuiWindowFlags_NoTitleBar | ImGuiWindowFlags_AlwaysAutoResize |
                         ImGuiWindowFlags_NoCollapse | ImGuiWindowFlags_NoSavedSettings)) {
            const bool isAngle = dimIsAngle_;
            const char* suffix = isAngle ? "\xC2\xB0" : skLenUnit();
            // Friendly title + symbol per dim type.
            const char* title = dimType_ == "diameter" ? "\xE2\x8C\x80 Diameter"
                              : dimType_ == "radius"   ? "R Radius"
                              : dimType_ == "angle"    ? "\xE2\x88\xA0 Angle"
                              : "Distance";
            // ---- header row: title (accent) + live measured readout on the right ----
            ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(pal.accent[0], pal.accent[1], pal.accent[2], 1));
            ImGui::TextUnformatted(dimEditCid_.empty() ? title : (std::string("Edit ") + title).c_str());
            ImGui::PopStyleColor();
            {
                std::string meas = isAngle ? fmtAng(dimMeasured_)
                                           : (fmtLen(dimMeasured_) + " " + skLenUnit());
                std::string lab = "now " + meas;
                float w = ImGui::CalcTextSize(lab.c_str()).x;
                ImGui::SameLine(0, 0);
                ImGui::SetCursorPosX(ImGui::GetCursorPosX() +
                    std::max(14.0f * sc, ImGui::GetContentRegionAvail().x - w));
                ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(pal.consoleDim[0], pal.consoleDim[1], pal.consoleDim[2], 1));
                ImGui::TextUnformatted(lab.c_str());
                ImGui::PopStyleColor();
            }
            ImGui::Separator();

            // ---- value row: [-] [ value ] unit [+] ----
            auto stepVal = [&](double delta) {
                std::string txt = dimBuf_.data();
                if (!txt.empty() && txt[0] == '=') return;   // don't mutate expressions
                // Steps are one DISPLAY unit: in IPS a length step is 1 inch (= 25.4 mm canonical).
                const double bareScale = (g_skIps && !isAngle) ? 25.4 : 1.0;
                double cur = dimMeasured_; std::string perr;
                if (ui::sketch::parseSketchDim(txt, cur, perr, isAngle, bareScale)) {}  // parse current text if valid
                cur += delta * ((isAngle) ? 1.0 : bareScale);
                std::snprintf(dimBuf_.data(), dimBuf_.size(), "%s", fmtLen(cur).c_str());
            };
            const float btn = ImGui::GetFrameHeight();
            if (ImGui::Button("-", ImVec2(btn, btn))) stepVal(-dimPopupStep_);
            ImGui::SameLine();
            ImGui::SetNextItemWidth(150 * sc);
            if (dimPopupFocus_) ImGui::SetKeyboardFocusHere();
            bool enter = ImGui::InputText("##dimval", dimBuf_.data(), dimBuf_.size(),
                                          ImGuiInputTextFlags_EnterReturnsTrue |
                                          (dimPopupFocus_ ? ImGuiInputTextFlags_AutoSelectAll : 0));
            dimPopupFocus_ = false;
            ImGui::SameLine(0, 4 * sc);
            ImGui::TextUnformatted(suffix);
            ImGui::SameLine();
            if (ImGui::Button("+", ImVec2(btn, btn))) stepVal(dimPopupStep_);

            // ---- options row: Driven + Arc condition ----
            ImGui::Checkbox("Driven (reference)", &dimPopupDriven_);
            if (dimEditCid_.empty() && dimType_ == "distance") {
                static const char* kConds[3] = {"Center", "Min (near rim)", "Max (far rim)"};
                bool condChanged = false;
                int shown = 0;
                for (auto& p : dimPicks_) {
                    if (p.isPoint) continue;
                    const SketchEntity* ce = entById(p.entId);
                    if (!ce || (ce->type != "circle" && ce->type != "arc")) continue;
                    int cur = p.cond == "min" ? 1 : p.cond == "max" ? 2 : 0;
                    ImGui::SetNextItemWidth(150 * sc);
                    std::string lbl = std::string(shown == 0 ? "Arc condition" : "Arc condition 2")
                                      + "##cond" + p.entId;
                    if (ImGui::Combo(lbl.c_str(), &cur, kConds, 3)) {
                        p.cond = cur == 1 ? "min" : cur == 2 ? "max" : "center";
                        condChanged = true;
                    }
                    ++shown;
                }
                if (condChanged) {
                    // Re-derive refs/cond AND the measured value from the updated picks, keeping the
                    // already-chosen axis. This routes through the same tangent-line math as the
                    // live preview, so min/max on overlapping or concentric circles reads correctly
                    // (the old incremental "+= adjNew - adjOld" assumed the additive centre-line
                    // model and was wrong for the directional h/v case).
                    dimBuildPending(dimPopupPos_, dimPlaceU_, dimPlaceV_, /*keepAxis=*/true);
                    std::snprintf(dimBuf_.data(), dimBuf_.size(), "%s", fmtLen(dimMeasured_).c_str());
                }
            }
            if (!dimError_.empty()) {
                ImGui::PushStyleColor(ImGuiCol_Text,
                    ImVec4(pal.statusErr[0], pal.statusErr[1], pal.statusErr[2], 1));
                ImGui::Text("! %s", dimError_.c_str());
                ImGui::PopStyleColor();
            }

            // ---- footer: primary OK (accent) + Cancel, right-aligned ----
            ImGui::Separator();
            const float okW = 64 * sc, cancelW = 64 * sc;
            ImGui::SetCursorPosX(ImGui::GetCursorPosX() +
                std::max(0.0f, ImGui::GetContentRegionAvail().x - okW - cancelW - 7 * sc));
            ImGui::PushStyleColor(ImGuiCol_Button, ImVec4(pal.accent[0], pal.accent[1], pal.accent[2], 1));
            ImGui::PushStyleColor(ImGuiCol_ButtonHovered, ImVec4(pal.accent[0], pal.accent[1], pal.accent[2], 0.85f));
            ImGui::PushStyleColor(ImGuiCol_ButtonActive, ImVec4(pal.accent[0], pal.accent[1], pal.accent[2], 0.70f));
            ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(pal.windowBg[0], pal.windowBg[1], pal.windowBg[2], 1));
            bool ok = ImGui::Button("OK", ImVec2(okW, 0)) || enter;
            ImGui::PopStyleColor(4);
            ImGui::SameLine();
            bool cancel = ImGui::Button("Cancel", ImVec2(cancelW, 0));
            if (ok) dimCommit(st);
            if (cancel || (ImGui::IsKeyPressed(ImGuiKey_Escape) && !ImGui::IsKeyDown(ImGuiKey_LeftCtrl))) {
                escHandled = true;   // C41 (T1): box close keeps Smart Dimension armed
                dimPopupOpen_ = false; dimError_.clear();
                if (dimEditCid_.empty()) { dimPicks_.clear(); dimHasPending_ = false; dimPending_ = nullptr; }
                dimEditCid_.clear();
            }
        }
        ImGui::End();
        ImGui::PopStyleVar(3);
    }

    // C50f: SolidWorks over-define pre-flight dialogs (add path: "Make Dimension Driven?";
    // edit path: "This value over-defines the sketch"). Both are opened by sketch_probe before
    // anything is committed.
    drawOverDefinePrompt(st);
    drawEditOverDefinePrompt(st);
    drawRelationOverDefinePrompt(st);

    // ---- Offset distance overlay (small InputFloat near the cursor; Enter -> side-pick stage) ----
    if (tool_ == SketchTool::Offset && offsetStage_ == 1) {
        ImGui::SetNextWindowPos(offsetOverlayPos_, ImGuiCond_Appearing);
        ImGui::SetNextWindowBgAlpha(0.98f);
        if (ImGui::Begin("##SketchOffsetDist", nullptr,
                         ImGuiWindowFlags_NoTitleBar | ImGuiWindowFlags_AlwaysAutoResize |
                         ImGuiWindowFlags_NoCollapse | ImGuiWindowFlags_NoSavedSettings)) {
            ImGui::TextUnformatted("Offset distance (mm)");
            ImGui::SetNextItemWidth(120);
            if (offsetFocusInput_) { ImGui::SetKeyboardFocusHere(); offsetFocusInput_ = false; }
            bool enter = ImGui::InputFloat("##offdist", &offsetDist_, 0.0f, 0.0f, "%.3f",
                                           ImGuiInputTextFlags_EnterReturnsTrue);
            bool ok = ImGui::Button("Next") || enter;
            ImGui::SameLine();
            bool cancel = ImGui::Button("Cancel");
            if (ok) { offsetDist_ = std::fabs(offsetDist_); offsetStage_ = 2; }
            if (cancel) { offsetStage_ = 0; }
        }
        ImGui::End();
    }

    // ---- C52: Sketch Fillet / Chamfer numeric input (always shown while the tool is armed) ----
    // A small always-on overlay near the status chip holds the radius (fillet) / distance (chamfer);
    // the value persists across corners and invocations. Editing it never disturbs the pick state —
    // the two lines are still picked by clicking in the viewport. Pinned top-left so it doesn't fight
    // the cursor while the user clicks corners (unlike Offset's one-shot box near the cursor).
    if (tool_ == SketchTool::SkFillet || tool_ == SketchTool::SkChamfer) {
        const bool fillet = (tool_ == SketchTool::SkFillet);
        ImGui::SetNextWindowPos(ImVec2(imgMin.x + 12, imgMin.y + 52), ImGuiCond_Appearing);
        ImGui::SetNextWindowBgAlpha(0.98f);
        if (ImGui::Begin(fillet ? "##SketchFilletParam" : "##SketchChamferParam", nullptr,
                         ImGuiWindowFlags_NoTitleBar | ImGuiWindowFlags_AlwaysAutoResize |
                         ImGuiWindowFlags_NoCollapse | ImGuiWindowFlags_NoSavedSettings)) {
            ImGui::TextUnformatted(fillet ? "Fillet radius (mm)" : "Chamfer distance (mm)");
            ImGui::SetNextItemWidth(120);
            float* val = fillet ? &skFilletRadius_ : &skChamferDist_;
            ImGui::InputFloat(fillet ? "##skfilletr" : "##skchamferd", val, 1.0f, 5.0f, "%.3f");
            if (*val < 0.001f) *val = 0.001f;   // engine requires a strictly positive value
        }
        ImGui::End();
    }

    // ---- C53: Linear / Circular Pattern numeric panel (count + spacing/angle, then Apply) ----
    // Shown once the entity selection is locked (Enter): Linear at stage 1, Circular at stage 2
    // (after the center pick). The dims persist across invocations. Apply builds the op; Cancel
    // backs out to re-pick. The viewport selection is frozen while the panel is up.
    bool linPanel  = (tool_ == SketchTool::LinPattern  && mpStage_ == 1);
    bool circPanel = (tool_ == SketchTool::CircPattern && mpStage_ == 2);
    if (linPanel || circPanel) {
        ImGui::SetNextWindowPos(ImVec2(imgMin.x + 12, imgMin.y + 52), ImGuiCond_Appearing);
        ImGui::SetNextWindowBgAlpha(0.98f);
        if (ImGui::Begin(linPanel ? "##SketchLinPattern" : "##SketchCircPattern", nullptr,
                         ImGuiWindowFlags_NoTitleBar | ImGuiWindowFlags_AlwaysAutoResize |
                         ImGuiWindowFlags_NoCollapse | ImGuiWindowFlags_NoSavedSettings)) {
            ImGui::Text(linPanel ? "Linear Pattern (%d entities)" : "Circular Pattern (%d entities)",
                        (int)mpSel_.size());
            ImGui::Separator();
            ImGui::SetNextItemWidth(120);
            ImGui::InputInt("Count", &patternCount_);
            if (patternCount_ < 2) patternCount_ = 2;   // engine requires count >= 2
            if (linPanel) {
                ImGui::SetNextItemWidth(120);
                ImGui::InputFloat("Spacing (mm)", &patternSpacing_, 1.0f, 10.0f, "%.3f");
                ImGui::SetNextItemWidth(180);
                ImGui::InputFloat2("Direction (u,v)", &patternDirU_, "%.2f");
            } else {
                ImGui::SetNextItemWidth(120);
                ImGui::InputFloat(patternEqualSpacing_ ? "Total angle (deg)" : "Step angle (deg)",
                                  &patternAngle_, 1.0f, 15.0f, "%.2f");
                ImGui::Checkbox("Equal spacing (full span)", &patternEqualSpacing_);
            }
            ImGui::Separator();
            if (ImGui::Button("Apply")) applyPatternOp(st, circPanel);
            ImGui::SameLine();
            if (ImGui::Button("Cancel")) { mpStage_ = circPanel ? 1 : 0; }
        }
        ImGui::End();
    }

    // ---- Sketch Text + Equation-Driven Curve parameter popups (engine entities text / eq_curve) ----
    drawTextPopup(st, escHandled);
    drawEqCurvePopup(st, escHandled);
    drawAirfoilPopup(st, escHandled);

    // ---- C50: Add Relation popup over an N-element selection (entities and/or point handles) ----
    // Classifies the snapshotted selection and offers the SolidWorks relations valid for it:
    // points -> coincident / horizontal (same Y) / vertical (same X); lines -> horizontal /
    // vertical / collinear / perpendicular(2) / parallel / equal / symmetric(3); circles/arcs ->
    // coradial / equal / concentric / tangent(2); line+circle -> tangent; point+line -> point_on /
    // midpoint; 2 same-kind subjects + 1 line -> symmetric (axis last). Pair relations chain to the
    // first ref (e0~e1, e0~e2, …); symmetric uses an explicit ordered [a,b,axis] ref list.
    if (relPopupOpen_) {
        for (const auto& h : relIds_) {
            const std::string base = h.substr(0, h.find('.'));
            // "origin" is the virtual fixed-origin handle (not in ents_) — keep it, don't purge.
            if (base != "origin" && !entById(base)) { relPopupOpen_ = false; relIds_.clear(); break; }
        }
    }
    if (relPopupOpen_) {
        // Classify: a handle with a '.' (or a bare point entity) is a POINT; bare line/circle ids
        // are entities. (Centers select as "eN.center" = point.) The bare "origin" is a point too.
        auto isPointHandle = [&](const std::string& h) -> bool {
            if (h == "origin") return true;
            if (h.find('.') != std::string::npos) return true;
            const SketchEntity* e = entById(h);
            return e && e->type == "point";
        };
        int nPts = 0, nLines = 0, nCircles = 0, nOther = 0;
        for (const auto& h : relIds_) {
            if (isPointHandle(h)) { ++nPts; continue; }
            const SketchEntity* e = entById(h);
            if (!e) { ++nOther; continue; }
            if (e->type == "line") ++nLines;
            else if (e->type == "circle" || e->type == "arc") ++nCircles;
            else ++nOther;
        }
        const int n = (int)relIds_.size();

        ImGui::SetNextWindowPos(relPopupPos_, ImGuiCond_Appearing);
        ImGui::SetNextWindowBgAlpha(0.98f);
        if (ImGui::Begin("##SketchRel", nullptr,
                         ImGuiWindowFlags_NoTitleBar | ImGuiWindowFlags_AlwaysAutoResize |
                         ImGuiWindowFlags_NoCollapse | ImGuiWindowFlags_NoSavedSettings)) {
            ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(pal.accent[0], pal.accent[1], pal.accent[2], 1));
            ImGui::Text("Add Relation  (%d selected)", n);
            ImGui::PopStyleColor();
            ImGui::Separator();
            bool any = false;
            // Emit a relation. mode: 'each' = one constraint per ref ($self-style single ref);
            // 'chain' = relate refs[0] to every other ref; 'pair' = exactly refs[0],refs[1].
            auto emit = [&](const char* label, const char* type, char mode) {
                any = true;
                if (!ImGui::MenuItem(label)) return;
                nlohmann::json ops = nlohmann::json::array();
                if (mode == 'e') {
                    for (const auto& h : relIds_)
                        ops.push_back({{"op", "sketch_add_constraint"}, {"sketch", fid_},
                            {"constraint", {{"type", type}, {"refs", nlohmann::json::array({h})}}}});
                } else if (mode == 'p') {
                    ops.push_back({{"op", "sketch_add_constraint"}, {"sketch", fid_},
                        {"constraint", {{"type", type}, {"refs", nlohmann::json::array({relIds_[0], relIds_[1]})}}}});
                } else {  // chain
                    for (size_t i = 1; i < relIds_.size(); ++i)
                        ops.push_back({{"op", "sketch_add_constraint"}, {"sketch", fid_},
                            {"constraint", {{"type", type}, {"refs", nlohmann::json::array({relIds_[0], relIds_[i]})}}}});
                }
                addRelationOps(st, ops, label);
            };

            // Emit a single multi-ref constraint with an explicit ordered ref list (used by
            // symmetric, whose engine refs are [subjectA, subjectB, axisLine] — axis LAST).
            auto emitOrdered = [&](const char* label, const char* type,
                                   const std::vector<std::string>& refs) {
                any = true;
                if (!ImGui::MenuItem(label)) return;
                nlohmann::json arr = nlohmann::json::array();
                for (const auto& r : refs) arr.push_back(r);
                nlohmann::json ops = nlohmann::json::array();
                ops.push_back({{"op", "sketch_add_constraint"}, {"sketch", fid_},
                    {"constraint", {{"type", type}, {"refs", arr}}}});
                addRelationOps(st, ops, label);
            };

            const bool onlyPts    = nPts == n;
            const bool onlyLines  = nLines == n;
            const bool onlyCircs  = nCircles == n;

            if (onlyPts && n >= 2) {
                emit("Coincident / Merge", "coincident", n == 2 ? 'p' : 'c');
                emit("Horizontal (align Y)", "horizontal", 'c');
                emit("Vertical (align X)",   "vertical",   'c');
            } else if (onlyLines && n == 1) {
                emit("Horizontal", "horizontal", 'e');
                emit("Vertical",   "vertical",   'e');
                emit("Fix",        "fix",        'e');
            } else if (onlyLines && n >= 2) {
                // Order mirrors SolidWorks' line context menu: H, V, Collinear, Perp, Parallel, Equal.
                emit("Horizontal (each)", "horizontal", 'e');
                emit("Vertical (each)",   "vertical",   'e');
                emit("Collinear", "collinear", 'c');
                if (n == 2) emit("Perpendicular", "perpendicular", 'p');
                emit("Parallel", "parallel", 'c');
                emit("Equal", "equal", 'c');
                // 3 lines selected: offer Symmetric with the LAST-picked line as the mirror axis
                // (relIds_ preserves pick order). subjects = the two earlier lines.
                if (n == 3)
                    emitOrdered("Symmetric (last line = axis)", "symmetric",
                                {relIds_[0], relIds_[1], relIds_[2]});
            } else if (onlyCircs && n >= 2) {
                emit("Collinear / Coradial", "coradial", 'c');  // same center + same radius
                emit("Equal", "equal", 'c');
                emit("Concentric", "concentric", 'c');
                if (n == 2) emit("Tangent", "tangent", 'p');    // curve <-> curve tangency
            } else if (n == 2 && nLines == 1 && nCircles == 1) {
                emit("Tangent", "tangent", 'p');
            } else if (n == 2 && nPts == 1 && nLines == 1) {
                // point + line. Two readings: point ON the line, or point at the line MIDPOINT.
                // Both expect refs [point, line]; reorder so the point handle is first.
                if (!isPointHandle(relIds_[0])) std::swap(relIds_[0], relIds_[1]);
                emit("Coincident (point on line)", "point_on", 'p');
                emit("Midpoint (point at line center)", "midpoint", 'p');
            } else if (n == 3 && nLines == 1 && (nPts == 2 || nCircles == 2)) {
                // Two same-kind subjects + one line: mirror the subjects across that line.
                // refs = [subjectA, subjectB, axisLine]; pull the lone line out to the end.
                std::vector<std::string> subs; std::string axis;
                for (const auto& h : relIds_) {
                    const SketchEntity* e = isPointHandle(h) ? nullptr : entById(h);
                    if (e && e->type == "line") axis = h; else subs.push_back(h);
                }
                if (subs.size() == 2 && !axis.empty())
                    emitOrdered("Symmetric (across line)", "symmetric", {subs[0], subs[1], axis});
            } else if (n == 1) {
                // The origin is implicitly fixed; "Fix" on it would be a dead no-op constraint.
                if (relIds_[0] != "origin") emit("Fix", "fix", 'e');
            }
            if (!any) ImGui::TextDisabled("No relations for this selection");
            ImGui::Separator();
            if (ImGui::MenuItem("Cancel") ||
                (relPopupOpen_ && ImGui::IsKeyPressed(ImGuiKey_Escape))) {
                escHandled = true;
                relPopupOpen_ = false; relIds_.clear();
            }
        }
        ImGui::End();
    }

    // ---- C41 (T1): catch-all Esc — disarm the armed tool / in-progress chain even when the
    // viewport image is NOT hovered. The more specific handlers above run first: the ones that
    // fully cancel leave tool_ == None (no-op here); the ones that keep the tool armed (Modify
    // box close, Smart Dim pick clear, Offset stage back-out) mark escHandled. Suppressed while
    // any text field owns the keyboard. Esc with no tool armed does nothing here.
    if (!escHandled && tool_ != SketchTool::None && !io.WantTextInput &&
        ImGui::IsKeyPressed(ImGuiKey_Escape))
        cancelTool();
}

}  // namespace ui
