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
#define skPal ui::sketch::sketchPalette()

namespace ui {
std::vector<std::string> SketchMode::addQuadLoop(SketchHost& st, const ImVec2 corners[4],
                             const char* relations[4], const std::string* cornerSnaps) {
    std::string ids[4];
    std::string prevId, firstId;
    for (int i = 0; i < 4; ++i) {
        int j = (i + 1) % 4;
        nlohmann::json cs = nlohmann::json::array();
        const char* rel = relations[i];
        // Single-ref auto relations (h/v) reference $self; pair relations (parallel/equal/perp) are
        // applied in a second pass below once all four ids exist.
        if (rel && (std::strcmp(rel, "horizontal") == 0 || std::strcmp(rel, "vertical") == 0))
            cs.push_back({{"type", rel}, {"refs", {"$self"}}});
        if (!prevId.empty())
            cs.push_back({{"type", "coincident"}, {"refs", {prevId + ".p2", "$self.p1"}}});
        // Corner pick snapped onto a vertex/origin -> keep that coincident on this edge's p1.
        if (cornerSnaps) pushSnapConstraint(cornerSnaps[i], "$self.p1", cs);
        nlohmann::json ent = {{"type", "line"},
            {"p1", nlohmann::json::array({corners[i].x, corners[i].y})},
            {"p2", nlohmann::json::array({corners[j].x, corners[j].y})}};
        nlohmann::json op = {{"op", "sketch_add_entity"}, {"sketch", fid_},
                             {"entity", ent}, {"constraints", cs}};
        // C50: one undo step for the whole rectangle — only the first batch pushes an undo snapshot.
        auto r = st.docMirror().applyOps(st.worker(), nlohmann::json::array({op}), /*coalesce=*/i != 0);
        if (!r.ok) { st.addLog("[sketch] quad line failed: " + r.error_message); break; }
        std::string nid = (r.id_map.is_object() && r.id_map.contains("$0"))
                        ? r.id_map["$0"].get<std::string>() : std::string();
        ids[i] = nid;
        if (i == 0) firstId = nid;
        prevId = nid;
        if (i == 3 && !firstId.empty() && !nid.empty()) {
            nlohmann::json clop = {{"op", "sketch_add_constraint"}, {"sketch", fid_},
                {"constraint", {{"type", "coincident"}, {"refs", {nid + ".p2", firstId + ".p1"}}}}};
            st.docMirror().applyOps(st.worker(), nlohmann::json::array({clop}), /*coalesce=*/true);
        }
        if (!r.aicm_path.empty()) { std::string le; st.loadDocMesh(r.aicm_path, le); }
    }
    // Second pass: pair relations on the two OPPOSITE-edge pairs (0<->2, 1<->3). The relation token
    // may combine "parallel"/"equal"/"perpendicular" (space-separated); we apply each once per pair
    // (only for i<2 so the 0/2 and 1/3 pairs aren't double-emitted). "perpendicular" relates the
    // pair's first edge to the adjacent edge i+1.
    auto pair = [&](const char* type, const std::string& a, const std::string& b) {
        if (a.empty() || b.empty()) return;
        nlohmann::json op = {{"op", "sketch_add_constraint"}, {"sketch", fid_},
            {"constraint", {{"type", type}, {"refs", {a, b}}}}};
        st.docMirror().applyOps(st.worker(), nlohmann::json::array({op}), /*coalesce=*/true);
    };
    for (int i = 0; i < 2; ++i) {
        const char* rel = relations[i];
        if (!rel) continue;
        std::string tok(rel);
        if (tok.find("parallel") != std::string::npos) pair("parallel", ids[i], ids[i + 2]);
        if (tok.find("equal") != std::string::npos)    pair("equal",    ids[i], ids[i + 2]);
        if (tok.find("perpendicular") != std::string::npos) pair("perpendicular", ids[i], ids[i + 1]);
    }
    resolve(st);
    return {ids[0], ids[1], ids[2], ids[3]};
}

// C42: N-line closed loop (generalized addQuadLoop without the per-edge h/v relations). Each edge
// gets a chained coincident to its predecessor's p2 (+ loop-closing coincident); equalEdges adds
// "equal" between consecutive edges so a Polygon stays regular-ish under drag.
std::vector<std::string> SketchMode::addPolyLoop(SketchHost& st, const std::vector<ImVec2>& corners,
                                                 bool equalEdges) {
    const size_t n = corners.size();
    if (n < 3) return {};
    std::vector<std::string> ids(n);
    std::string prevId, firstId;
    for (size_t i = 0; i < n; ++i) {
        size_t j = (i + 1) % n;
        nlohmann::json cs = nlohmann::json::array();
        if (!prevId.empty())
            cs.push_back({{"type", "coincident"}, {"refs", {prevId + ".p2", "$self.p1"}}});
        nlohmann::json ent = {{"type", "line"},
            {"p1", nlohmann::json::array({corners[i].x, corners[i].y})},
            {"p2", nlohmann::json::array({corners[j].x, corners[j].y})}};
        nlohmann::json op = {{"op", "sketch_add_entity"}, {"sketch", fid_},
                             {"entity", ent}, {"constraints", cs}};
        // C50: one undo step for the whole polygon — only the first batch pushes an undo snapshot.
        auto r = st.docMirror().applyOps(st.worker(), nlohmann::json::array({op}), /*coalesce=*/i != 0);
        if (!r.ok) { st.addLog("[sketch] poly line failed: " + r.error_message); break; }
        std::string nid = (r.id_map.is_object() && r.id_map.contains("$0"))
                        ? r.id_map["$0"].get<std::string>() : std::string();
        ids[i] = nid;
        if (i == 0) firstId = nid;
        prevId = nid;
        if (i == n - 1 && !firstId.empty() && !nid.empty()) {
            nlohmann::json clop = {{"op", "sketch_add_constraint"}, {"sketch", fid_},
                {"constraint", {{"type", "coincident"}, {"refs", {nid + ".p2", firstId + ".p1"}}}}};
            st.docMirror().applyOps(st.worker(), nlohmann::json::array({clop}), /*coalesce=*/true);
        }
        if (!r.aicm_path.empty()) { std::string le; st.loadDocMesh(r.aicm_path, le); }
    }
    if (equalEdges) {
        for (size_t i = 0; i + 1 < n; ++i) {
            if (ids[i].empty() || ids[i + 1].empty()) continue;
            nlohmann::json op = {{"op", "sketch_add_constraint"}, {"sketch", fid_},
                {"constraint", {{"type", "equal"}, {"refs", {ids[i], ids[i + 1]}}}}};
            st.docMirror().applyOps(st.worker(), nlohmann::json::array({op}), /*coalesce=*/true);
        }
    }
    resolve(st);
    return ids;
}

// C51: straight slot as real geometry (see header). Builds the capsule from the spine endpoints
// p1/p2 (the arc centers) and the full width: 2 side lines tangent-offset by half-width, 2 cap
// arcs (the outer semicircles), and a construction centerline. Endpoints are chained by coincident
// closure; the centerline ends pin to the arc centers; the cap radii are equal. All five batches
// coalesce into ONE undo step.
void SketchMode::addDecomposedSlot(SketchHost& st, const ImVec2& p1, const ImVec2& p2, float width,
                                   const std::string& snap1, const std::string& snap2) {
    const float R2D = 57.29577951f, D2R = 1.0f / 57.29577951f;
    float hw = width * 0.5f;
    float axisDeg = std::atan2(p2.y - p1.y, p2.x - p1.x) * R2D;
    float nx = -std::sin(axisDeg * D2R), ny = std::cos(axisDeg * D2R);   // unit +normal
    ImVec2 A1(p1.x + nx * hw, p1.y + ny * hw), B1(p2.x + nx * hw, p2.y + ny * hw);   // l1: A1->B1
    ImVec2 A2(p1.x - nx * hw, p1.y - ny * hw), B2(p2.x - nx * hw, p2.y - ny * hw);   // l2: A2->B2

    bool first = true;
    auto add = [&](const nlohmann::json& entity) -> std::string {
        nlohmann::json op = {{"op", "sketch_add_entity"}, {"sketch", fid_}, {"entity", entity}};
        auto r = st.docMirror().applyOps(st.worker(), nlohmann::json::array({op}), /*coalesce=*/!first);
        first = false;
        if (!r.ok) { st.addLog("[sketch] slot part failed: " + r.error_message); return std::string(); }
        if (!r.aicm_path.empty()) { std::string le; st.loadDocMesh(r.aicm_path, le); }
        return (r.id_map.is_object() && r.id_map.contains("$0"))
             ? r.id_map["$0"].get<std::string>() : std::string();
    };
    std::string l1 = add({{"type", "line"}, {"p1", {A1.x, A1.y}}, {"p2", {B1.x, B1.y}}});
    std::string l2 = add({{"type", "line"}, {"p1", {A2.x, A2.y}}, {"p2", {B2.x, B2.y}}});
    // c1 at p1: start=axis+90 (=A1) -> end=axis-90 (=A2), CCW (outer bulge away from p2).
    std::string c1 = add({{"type", "arc"}, {"center", {p1.x, p1.y}}, {"radius", hw},
                          {"start", axisDeg + 90.0f}, {"end", axisDeg - 90.0f}, {"ccw", true}});
    // c2 at p2: start=axis-90 (=B2) -> end=axis+90 (=B1), CCW (outer bulge away from p1).
    std::string c2 = add({{"type", "arc"}, {"center", {p2.x, p2.y}}, {"radius", hw},
                          {"start", axisDeg - 90.0f}, {"end", axisDeg + 90.0f}, {"ccw", true}});
    std::string sp = add({{"type", "line"}, {"construction", true},
                          {"p1", {p1.x, p1.y}}, {"p2", {p2.x, p2.y}}});
    if (l1.empty() || l2.empty() || c1.empty() || c2.empty() || sp.empty()) { resolve(st); return; }

    auto con = [&](const nlohmann::json& c) {
        nlohmann::json op = {{"op", "sketch_add_constraint"}, {"sketch", fid_}, {"constraint", c}};
        st.docMirror().applyOps(st.worker(), nlohmann::json::array({op}), /*coalesce=*/true);
    };
    con({{"type", "coincident"}, {"refs", {l1 + ".p1", c1 + ".start"}}});   // A1
    con({{"type", "coincident"}, {"refs", {l1 + ".p2", c2 + ".end"}}});     // B1
    con({{"type", "coincident"}, {"refs", {l2 + ".p1", c1 + ".end"}}});     // A2
    con({{"type", "coincident"}, {"refs", {l2 + ".p2", c2 + ".start"}}});   // B2
    con({{"type", "coincident"}, {"refs", {sp + ".p1", c1 + ".center"}}});  // centerline -> centers
    con({{"type", "coincident"}, {"refs", {sp + ".p2", c2 + ".center"}}});
    con({{"type", "equal"}, {"refs", {c1, c2}}});                           // uniform cap radius
    // SolidWorks-style slot: each side line is tangent to BOTH end caps so the capsule shape
    // holds under drag (and the sides stay parallel — implied by the 4 tangents + equal radii,
    // so no explicit parallel is emitted). These were previously omitted because our solver's
    // linearized rank test reports a tangent whose contact point coincides with the shared
    // endpoint as "redundant", and the session then flagged the slot falsely Over Defined. That
    // is fixed (engine/doc/session.py: a redundant geometric relation no longer over-defines —
    // only a redundant driving dimension does), so the real tangency relations are restored.
    con({{"type", "tangent"}, {"refs", {l1, c1}}});
    con({{"type", "tangent"}, {"refs", {l1, c2}}});
    con({{"type", "tangent"}, {"refs", {l2, c1}}});
    con({{"type", "tangent"}, {"refs", {l2, c2}}});
    // Placement snaps on the two spine endpoints -> constraints on the cap-arc centers.
    nlohmann::json endCs = nlohmann::json::array();
    pushSnapConstraint(snap1, c1 + ".center", endCs);
    pushSnapConstraint(snap2, c2 + ".center", endCs);
    for (const auto& c : endCs) con(c);
    resolve(st);
}

// ---------------------------------------------------------------------------
// enter / exit
// ---------------------------------------------------------------------------

void SketchMode::drawTextPopup(SketchHost& st, bool& escHandled) {
    if (!textPopupOpen_) return;
    const auto& pal = skPal;
    ImGui::SetNextWindowPos(textPopupPos_, ImGuiCond_Appearing);
    ImGui::SetNextWindowBgAlpha(0.98f);
    if (ImGui::Begin("##SketchText", nullptr,
                     ImGuiWindowFlags_NoTitleBar | ImGuiWindowFlags_AlwaysAutoResize |
                     ImGuiWindowFlags_NoCollapse | ImGuiWindowFlags_NoSavedSettings)) {
        ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(pal.accent[0], pal.accent[1], pal.accent[2], 1));
        ImGui::TextUnformatted("Sketch Text");
        ImGui::PopStyleColor();
        ImGui::Separator();
        ImGui::TextUnformatted("Text");
        ImGui::SetNextItemWidth(220);
        if (textFocus_) { ImGui::SetKeyboardFocusHere(); textFocus_ = false; }
        bool enter = ImGui::InputText("##textstr", textBuf_, sizeof(textBuf_),
                                      ImGuiInputTextFlags_EnterReturnsTrue);
        ImGui::SetNextItemWidth(120);
        ImGui::InputFloat("Height (mm)", &textHeight_, 1.0f, 5.0f, "%.3f");
        if (textHeight_ < 0.001f) textHeight_ = 0.001f;   // engine requires height > 0
        ImGui::Separator();
        const bool empty = (textBuf_[0] == '\0');
        ImGui::BeginDisabled(empty);
        bool ok = ImGui::Button("OK") || (enter && !empty);
        ImGui::EndDisabled();
        ImGui::SameLine();
        bool escPressed = ImGui::IsKeyPressed(ImGuiKey_Escape);
        bool cancel = ImGui::Button("Cancel") || escPressed;
        if (escPressed) escHandled = true;   // suppress the catch-all Esc (it would resetTool)
        if (empty && enter)
            st.toast(ui::sketch::ToastLevel::Info, "Text: enter a non-empty string");
        if (ok && !empty) {
            nlohmann::json ent = {{"type", "text"}, {"construction", false},
                {"p", nlohmann::json::array({textAnchorUV_.x, textAnchorUV_.y})},
                {"text", std::string(textBuf_)},
                {"height", (double)textHeight_}};
            addEntity(st, ent, nlohmann::json::array());
            textPopupOpen_ = false;
            resetTool(); tool_ = SketchTool::Text;   // stay armed for another placement (one-shot)
        } else if (cancel) {
            textPopupOpen_ = false;
            // keep the tool armed so the next click can place again
        }
    }
    ImGui::End();
}

// Equation-Driven Curve: a borderless popup collecting x(t)/y(t) expression strings + the t range
// for an "eq_curve" entity (origin-relative; no viewport pick). OK emits
// {"type":"eq_curve","construction":false,"xt":...,"yt":...,"t1":...,"t2":...}. Preset buttons fill
// the fields with a circle / sine wave. A closed curve (cos/sin over 0..2pi) extrudes as a region.
void SketchMode::drawEqCurvePopup(SketchHost& st, bool& escHandled) {
    if (!eqPopupOpen_) return;
    const auto& pal = skPal;
    ImGui::SetNextWindowPos(eqPopupPos_, ImGuiCond_Appearing);
    ImGui::SetNextWindowBgAlpha(0.98f);
    if (ImGui::Begin("##SketchEqCurve", nullptr,
                     ImGuiWindowFlags_NoTitleBar | ImGuiWindowFlags_AlwaysAutoResize |
                     ImGuiWindowFlags_NoCollapse | ImGuiWindowFlags_NoSavedSettings)) {
        ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(pal.accent[0], pal.accent[1], pal.accent[2], 1));
        ImGui::TextUnformatted("Equation Driven Curve");
        ImGui::PopStyleColor();
        ImGui::Separator();
        // Presets fill the expression fields (parameter t in radians).
        if (ImGui::SmallButton("Circle")) {
            std::snprintf(eqXt_, sizeof(eqXt_), "5*cos(t)");
            std::snprintf(eqYt_, sizeof(eqYt_), "5*sin(t)");
            eqT1_ = 0.0f; eqT2_ = 6.2831853f;
        }
        ImGui::SameLine();
        if (ImGui::SmallButton("Sine wave")) {
            std::snprintf(eqXt_, sizeof(eqXt_), "t");
            std::snprintf(eqYt_, sizeof(eqYt_), "5*sin(t)");
            eqT1_ = 0.0f; eqT2_ = 6.2831853f;
        }
        ImGui::Separator();
        ImGui::SetNextItemWidth(220);
        if (eqFocus_) { ImGui::SetKeyboardFocusHere(); eqFocus_ = false; }
        ImGui::InputText("x(t)", eqXt_, sizeof(eqXt_));
        ImGui::SetNextItemWidth(220);
        ImGui::InputText("y(t)", eqYt_, sizeof(eqYt_));
        ImGui::SetNextItemWidth(120);
        ImGui::InputFloat("t start", &eqT1_, 0.0f, 0.0f, "%.6f");
        ImGui::SetNextItemWidth(120);
        ImGui::InputFloat("t end (rad)", &eqT2_, 0.0f, 0.0f, "%.6f");
        ImGui::Separator();
        const bool empty = (eqXt_[0] == '\0' || eqYt_[0] == '\0');
        const bool badRange = (eqT2_ == eqT1_);
        ImGui::BeginDisabled(empty || badRange);
        bool ok = ImGui::Button("OK");
        ImGui::EndDisabled();
        ImGui::SameLine();
        bool escPressed = ImGui::IsKeyPressed(ImGuiKey_Escape);
        bool cancel = ImGui::Button("Cancel") || escPressed;
        if (escPressed) escHandled = true;   // suppress the catch-all Esc (it would also resetTool)
        if (ok && !empty && !badRange) {
            nlohmann::json ent = {{"type", "eq_curve"}, {"construction", false},
                {"xt", std::string(eqXt_)}, {"yt", std::string(eqYt_)},
                {"t1", (double)eqT1_}, {"t2", (double)eqT2_}};
            addEntity(st, ent, nlohmann::json::array());
            eqPopupOpen_ = false;
            resetTool();   // one entity per invocation, like Polygon/Ellipse
        } else if (cancel) {
            eqPopupOpen_ = false;
            resetTool();
        }
    }
    ImGui::End();
}

// Airfoil calculator: generate a NACA 4-/5-digit section (or load a .dat file), scaled to the
// chord and rotated by the angle of attack, with a live preview. OK emits the profile as chained
// splines via addAirfoil. Origin-relative (the leading edge sits at the sketch origin), so there
// is no viewport pick — the popup opens straight from the ribbon command, like Eq-Driven Curve.
void SketchMode::drawAirfoilPopup(SketchHost& st, bool& escHandled) {
    if (!afPopupOpen_) return;
    const auto& pal = skPal;
    ImGui::SetNextWindowPos(afPopupPos_, ImGuiCond_Appearing);
    ImGui::SetNextWindowBgAlpha(0.98f);
    if (ImGui::Begin("##SketchAirfoil", nullptr,
                     ImGuiWindowFlags_NoTitleBar | ImGuiWindowFlags_AlwaysAutoResize |
                     ImGuiWindowFlags_NoCollapse | ImGuiWindowFlags_NoSavedSettings)) {
        ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(pal.accent[0], pal.accent[1], pal.accent[2], 1));
        ImGui::TextUnformatted("Airfoil Calculator");
        ImGui::PopStyleColor();
        ImGui::Separator();

        const char* kSeries[] = {"NACA 4-digit", "NACA 5-digit", "Load .dat file"};
        ImGui::SetNextItemWidth(220);
        ImGui::Combo("Series", &afSeries_, kSeries, IM_ARRAYSIZE(kSeries));

        if (afSeries_ == 2) {
            // .dat import: a Browse button + the chosen file name.
            if (ImGui::Button("Browse .dat\xE2\x80\xA6")) {
                std::string pick = platform::openCadFile();
                if (!pick.empty()) afDatPath_ = pick;
            }
            ImGui::SameLine();
            if (afDatPath_.empty()) ImGui::TextDisabled("no file chosen");
            else {
                size_t slash = afDatPath_.find_last_of("/\\");
                ImGui::TextUnformatted(slash == std::string::npos ? afDatPath_.c_str()
                                                                  : afDatPath_.c_str() + slash + 1);
            }
        } else {
            // NACA code (presets help the user remember the layout).
            ImGui::SetNextItemWidth(120);
            if (afFocus_) { ImGui::SetKeyboardFocusHere(); afFocus_ = false; }
            ImGui::InputText(afSeries_ == 0 ? "Code (MPTT)" : "Code (LPQTT)",
                             afCode_, sizeof(afCode_), ImGuiInputTextFlags_CharsDecimal);
            if (afSeries_ == 0) {
                ImGui::SameLine();
                if (ImGui::SmallButton("2412")) std::snprintf(afCode_, sizeof(afCode_), "2412");
                ImGui::SameLine();
                if (ImGui::SmallButton("0012")) std::snprintf(afCode_, sizeof(afCode_), "0012");
            } else {
                ImGui::SameLine();
                if (ImGui::SmallButton("23012")) std::snprintf(afCode_, sizeof(afCode_), "23012");
            }
            ImGui::SetNextItemWidth(120);
            ImGui::SliderInt("Points/surface", &afPoints_, 20, 200);
            ImGui::Checkbox("Closed (sharp) trailing edge", &afClosedTE_);
        }

        ImGui::SetNextItemWidth(120);
        ImGui::InputFloat("Chord (mm)", &afChord_, 1.0f, 10.0f, "%.2f");
        if (afChord_ < 0.001f) afChord_ = 0.001f;
        ImGui::SetNextItemWidth(120);
        ImGui::InputFloat("Angle of attack", &afAoA_, 0.5f, 5.0f, "%.2f\xC2\xB0");

        // Generate the profile from the current parameters (cheap; recomputed each frame so the
        // preview tracks edits live).
        airfoil::Profile prof;
        if (afSeries_ == 0)      prof = airfoil::naca4(afCode_, afChord_, afAoA_, afPoints_, afClosedTE_);
        else if (afSeries_ == 1) prof = airfoil::naca5(afCode_, afChord_, afAoA_, afPoints_, afClosedTE_);
        else if (afDatPath_.empty()) prof.error = "choose a .dat file";
        else                     prof = airfoil::fromDatFile(afDatPath_, afChord_, afAoA_);

        // ---- live preview ----
        ImGui::Separator();
        const float pw = 240.0f, ph = 90.0f;
        ImVec2 org = ImGui::GetCursorScreenPos();
        ImDrawList* dl = ImGui::GetWindowDrawList();
        dl->AddRectFilled(org, ImVec2(org.x + pw, org.y + ph), col(pal.overlayBg), 4.0f);
        dl->AddRect(org, ImVec2(org.x + pw, org.y + ph), col(pal.overlayBorder), 4.0f);
        if (prof.ok()) {
            // Fit the profile bbox into the preview rect (uniform scale, Y up).
            double xmn = 1e30, xmx = -1e30, ymn = 1e30, ymx = -1e30;
            auto bound = [&](const std::vector<std::array<double, 2>>& s) {
                for (const auto& q : s) { xmn = std::min(xmn, q[0]); xmx = std::max(xmx, q[0]);
                                          ymn = std::min(ymn, q[1]); ymx = std::max(ymx, q[1]); }
            };
            bound(prof.upper); bound(prof.lower);
            double sx = (xmx - xmn) > 1e-9 ? (pw - 16) / (xmx - xmn) : 1.0;
            double sy = (ymx - ymn) > 1e-9 ? (ph - 16) / (ymx - ymn) : 1.0;
            double s = std::min(sx, sy);
            double cx = 0.5 * (xmn + xmx), cy = 0.5 * (ymn + ymx);
            auto map = [&](const std::array<double, 2>& q) -> ImVec2 {
                return ImVec2(float(org.x + pw * 0.5 + (q[0] - cx) * s),
                              float(org.y + ph * 0.5 - (q[1] - cy) * s));   // flip Y
            };
            auto stroke = [&](const std::vector<std::array<double, 2>>& sv) {
                for (size_t i = 1; i < sv.size(); ++i)
                    dl->AddLine(map(sv[i - 1]), map(sv[i]), col(pal.accent), 1.5f);
            };
            stroke(prof.upper); stroke(prof.lower);
            // close the TE visually
            dl->AddLine(map(prof.upper.back()), map(prof.lower.back()), col(pal.accent), 1.5f);
        } else {
            dl->AddText(ImVec2(org.x + 8, org.y + ph * 0.5f - 7), col(pal.statusErr),
                        prof.error.c_str());
        }
        ImGui::Dummy(ImVec2(pw, ph));
        if (prof.ok())
            ImGui::TextDisabled("%s  \xC2\xB7  %d pts  \xC2\xB7  %s TE", prof.name.c_str(),
                                int(prof.upper.size() + prof.lower.size()),
                                prof.sharpTE ? "sharp" : "open");

        ImGui::Separator();
        ImGui::BeginDisabled(!prof.ok());
        bool ok = ImGui::Button("OK");
        ImGui::EndDisabled();
        ImGui::SameLine();
        bool escPressed = ImGui::IsKeyPressed(ImGuiKey_Escape);
        bool cancel = ImGui::Button("Cancel") || escPressed;
        if (escPressed) escHandled = true;   // suppress the catch-all Esc (it would also resetTool)
        if (ok && prof.ok()) {
            addAirfoil(st, prof.upper, prof.lower, prof.sharpTE);
            st.toast(ui::sketch::ToastLevel::Info, prof.name + " airfoil added");
            afPopupOpen_ = false;
            resetTool();   // one profile per invocation
        } else if (cancel) {
            afPopupOpen_ = false;
            resetTool();
        }
    }
    ImGui::End();
}

// Emit a generated airfoil as real sketch geometry: the upper and lower surfaces become two
// through-point splines (LE->TE each), chained into one closed loop. A short trailing-edge line
// closes an open TE; a sharp TE shares the endpoint directly. Coincident relations tie the
// surfaces at the leading and trailing edges. All batches coalesce into ONE undo step, mirroring
// addDecomposedSlot. The C++ core profile builder now lifts the closed spline loop into a face,
// so the result extrudes/revolves/lofts like any other closed profile.
void SketchMode::addAirfoil(SketchHost& st, const std::vector<std::array<double, 2>>& upper,
                            const std::vector<std::array<double, 2>>& lower, bool sharpTE) {
    if (upper.size() < 2 || lower.size() < 2) return;
    bool first = true;
    auto add = [&](const nlohmann::json& entity) -> std::string {
        nlohmann::json op = {{"op", "sketch_add_entity"}, {"sketch", fid_}, {"entity", entity}};
        auto r = st.docMirror().applyOps(st.worker(), nlohmann::json::array({op}), /*coalesce=*/!first);
        first = false;
        if (!r.ok) { st.addLog("[sketch] airfoil part failed: " + r.error_message); return std::string(); }
        if (!r.aicm_path.empty()) { std::string le; st.loadDocMesh(r.aicm_path, le); }
        return (r.id_map.is_object() && r.id_map.contains("$0"))
             ? r.id_map["$0"].get<std::string>() : std::string();
    };
    auto ptsJson = [](const std::vector<std::array<double, 2>>& s) {
        nlohmann::json a = nlohmann::json::array();
        for (const auto& q : s) a.push_back(nlohmann::json::array({q[0], q[1]}));
        return a;
    };
    std::string up = add({{"type", "spline"}, {"points", ptsJson(upper)}});
    std::string lo = add({{"type", "spline"}, {"points", ptsJson(lower)}});
    std::string te;   // optional trailing-edge closing line (open TE only)
    if (!sharpTE)
        te = add({{"type", "line"},
                  {"p1", {upper.back()[0], upper.back()[1]}},
                  {"p2", {lower.back()[0], lower.back()[1]}}});
    if (up.empty() || lo.empty() || (!sharpTE && te.empty())) { resolve(st); return; }

    auto con = [&](const nlohmann::json& c) {
        nlohmann::json op = {{"op", "sketch_add_constraint"}, {"sketch", fid_}, {"constraint", c}};
        st.docMirror().applyOps(st.worker(), nlohmann::json::array({op}), /*coalesce=*/true);
    };
    const std::string uLE = up + ".p0";
    const std::string uTE = up + ".p" + std::to_string(upper.size() - 1);
    const std::string lLE = lo + ".p0";
    const std::string lTE = lo + ".p" + std::to_string(lower.size() - 1);
    con({{"type", "coincident"}, {"refs", {uLE, lLE}}});           // shared leading edge
    if (sharpTE) {
        con({{"type", "coincident"}, {"refs", {uTE, lTE}}});       // shared sharp trailing edge
    } else {
        con({{"type", "coincident"}, {"refs", {te + ".p1", uTE}}});
        con({{"type", "coincident"}, {"refs", {te + ".p2", lTE}}});
    }
    resolve(st);
}

// C52: apply one sketch_fillet / sketch_chamfer op for the held corner (e1=corner1Id_, e2=line2).
// The engine round-trips like every other sketch-editing op (applyOps -> loadDocMesh -> resolve).
// On an OpError (unsupported_entity / fillet_failed / chamfer_failed / invalid_op) the sketch is
// NOT mutated; we toast the engine message and reset the first pick so the user starts a fresh
// pair. On success the new arc/line appears and the sketch re-solves; we stay armed for the next
// corner (SolidWorks keeps filleting until Esc).
void SketchMode::snapPoint(float& u, float& v, std::string& snapHandle, SnapInfer* infer) const {
    snapHandle.clear();
    const ImVec2 cur = uvToScreen(u, v);   // raw cursor (snap distances measure from here)
    // Visit every snappable POINT (handle, u, v) — point snap + alignment share this set.
    auto forEachPoint = [&](const std::function<void(const std::string&, float, float)>& fn) {
        fn("origin", 0.0f, 0.0f);   // the sketch origin is a first-class snap/anchor target
        for (const auto& e : ents_) {
            if (e.type == "line" || e.type == "slot") {
                fn(e.id + ".p1", e.p1[0], e.p1[1]);
                fn(e.id + ".p2", e.p2[0], e.p2[1]);
            } else if (e.type == "circle" || e.type == "arc" || e.type == "arc_slot" ||
                       e.type == "ellipse") {
                fn(e.id + ".center", e.center[0], e.center[1]);
            } else if (e.type == "point") {
                fn(e.id + ".p", e.p1[0], e.p1[1]);
            } else if (e.type == "spline" || e.type == "style_spline") {  // C42: sub-handles p0..pN (sketch_solver convention; style_spline = control poles)
                for (size_t i = 0; i < e.pts.size(); ++i)
                    fn(e.id + ".p" + std::to_string(i), e.pts[i].x, e.pts[i].y);
            }
        }
    };
    // 1) Endpoint/center/point coincident snap within kSnapPx (screen space) — highest priority.
    float best = kSnapPx * kSnapPx;
    forEachPoint([&](const std::string& handle, float eu, float ev) {
        ImVec2 s = uvToScreen(eu, ev);
        float dx = s.x - cur.x, dy = s.y - cur.y;
        float d2 = dx * dx + dy * dy;
        if (d2 < best) { best = d2; snapHandle = handle; u = eu; v = ev; }
    });
    if (!inferenceOn_) return;   // master toggle: everything below is C49 inference
    // 2) Line-midpoint snap ("mid:<id>") — only when no point snap fired.
    if (snapHandle.empty()) {
        best = kSnapPx * kSnapPx;
        for (const auto& e : ents_) {
            if (e.type != "line") continue;
            float mu2 = (e.p1[0] + e.p2[0]) * 0.5f, mv2 = (e.p1[1] + e.p2[1]) * 0.5f;
            ImVec2 s = uvToScreen(mu2, mv2);
            float dx = s.x - cur.x, dy = s.y - cur.y;
            float d2 = dx * dx + dy * dy;
            if (d2 < best) { best = d2; snapHandle = "mid:" + e.id; u = mu2; v = mv2; }
        }
    }
    // 3) Circle/arc RIM snap ("rim:<id>"): within kSnapPx of the rim (full-circle metric, like
    //    hitTestEntity), pulled to the exact rim along the center->cursor ray. Tiny circles
    //    (rim within 2*kSnapPx of the center) are skipped — the center snap owns those.
    if (snapHandle.empty()) {
        best = kSnapPx;          // compare |dist - r| in px (1D)
        for (const auto& e : ents_) {
            if (e.type != "circle" && e.type != "arc") continue;
            ImVec2 cc = uvToScreen(e.center[0], e.center[1]);
            ImVec2 rimRef = uvToScreen(e.center[0] + e.radius, e.center[1]);
            float rpx = std::hypot(rimRef.x - cc.x, rimRef.y - cc.y);
            if (rpx < 2.0f * kSnapPx) continue;
            float dm = std::hypot(cur.x - cc.x, cur.y - cc.y);
            float d = std::fabs(dm - rpx);
            if (d < best) {
                float a = std::atan2(v - e.center[1], u - e.center[0]);
                best = d; snapHandle = "rim:" + e.id;
                u = e.center[0] + e.radius * std::cos(a);
                v = e.center[1] + e.radius * std::sin(a);
            }
        }
    }
    // 4) Alignment inference (positional only — SolidWorks-style dashed lines, no constraint):
    //    cursor u (or v) within kSnapPx of a source point's u (or v), the point itself clearly
    //    OUTSIDE the coincident radius (else the dashed line degenerates / fights the point snap).
    if (infer && snapHandle.empty()) {
        const float pxPerU = std::hypot(uAxisS_.x, uAxisS_.y);
        const float pxPerV = std::hypot(vAxisS_.x, vAxisS_.y);
        float bestU = kSnapPx, bestV = kSnapPx;   // screen-px distance along each axis
        forEachPoint([&](const std::string& handle, float eu, float ev) {
            ImVec2 s = uvToScreen(eu, ev);
            float d2 = (s.x - cur.x) * (s.x - cur.x) + (s.y - cur.y) * (s.y - cur.y);
            if (d2 < (2.0f * kSnapPx) * (2.0f * kSnapPx)) return;  // too close to the point itself
            float dU = std::fabs(eu - u) * pxPerU;
            float dV = std::fabs(ev - v) * pxPerV;
            if (dU < bestU) { bestU = dU; infer->alignU = true; infer->srcU = ImVec2(eu, ev);
                              infer->srcUHandle = handle; }
            if (dV < bestV) { bestV = dV; infer->alignV = true; infer->srcV = ImVec2(eu, ev);
                              infer->srcVHandle = handle; }
        });
        if (infer->alignU) u = infer->srcU.x;
        if (infer->alignV) v = infer->srcV.y;
    }
}

int SketchMode::hitTestHandle(const ImVec2& s, std::string& handleOut, float& uOut, float& vOut) const {
    float best = kHandlePx * kHandlePx;
    int hit = -1;
    auto consider = [&](const std::string& handle, float eu, float ev) {
        ImVec2 p = uvToScreen(eu, ev);
        float dx = p.x - s.x, dy = p.y - s.y;
        float d2 = dx * dx + dy * dy;
        if (d2 < best) { best = d2; handleOut = handle; uOut = eu; vOut = ev; hit = 1; }
    };
    for (const auto& e : ents_) {
        if (e.type == "line" || e.type == "slot") {
            consider(e.id + ".p1", e.p1[0], e.p1[1]);
            consider(e.id + ".p2", e.p2[0], e.p2[1]);
        } else if (e.type == "circle" || e.type == "arc" || e.type == "arc_slot" ||
                   e.type == "ellipse") {
            consider(e.id + ".center", e.center[0], e.center[1]);
            // C-bug1: a draggable RIM handle (resize radius) for circles/arcs, plus arc endpoint
            // angle handles. Computed in screen space then mapped back to (u,v) so the rim handle
            // tracks the actual circle under the cursor regardless of zoom / anisotropic basis.
            if (e.type == "circle" || e.type == "arc") {
                ImVec2 cc = uvToScreen(e.center[0], e.center[1]);
                ImVec2 rimRef = uvToScreen(e.center[0] + e.radius, e.center[1]);
                float rpx = std::hypot(rimRef.x - cc.x, rimRef.y - cc.y);
                if (rpx > 3.0f) {   // skip sub-pixel circles
                    float dxS = s.x - cc.x, dyS = s.y - cc.y;
                    float dS = std::hypot(dxS, dyS);
                    if (dS > 1e-3f) {
                        ImVec2 rimS(cc.x + dxS / dS * rpx, cc.y + dyS / dS * rpx);
                        float rimU, rimV; screenToUV(rimS, rimU, rimV);
                        if (e.type == "circle") {
                            consider(e.id + ".rim", rimU, rimV);
                        } else {
                            // Arc: endpoint handles always; the rim handle only inside the sweep.
                            // Registered center-first, endpoints-before-rim so endpoints win a tie.
                            const float D2R = 3.14159265f / 180.0f;
                            consider(e.id + ".start_angle",
                                     e.center[0] + e.radius * std::cos(e.start * D2R),
                                     e.center[1] + e.radius * std::sin(e.start * D2R));
                            consider(e.id + ".end_angle",
                                     e.center[0] + e.radius * std::cos(e.end * D2R),
                                     e.center[1] + e.radius * std::sin(e.end * D2R));
                            float rimAng = std::atan2(rimV - e.center[1],
                                                      rimU - e.center[0])
                                           * 180.0f / 3.14159265f;
                            if (rimAng < 0) rimAng += 360.0f;
                            if (arcSweepContains(e, rimAng))
                                consider(e.id + ".rim", rimU, rimV);
                        }
                    }
                }
            }
        } else if (e.type == "point") {
            consider(e.id + ".p", e.p1[0], e.p1[1]);
        } else if (e.type == "spline" || e.type == "style_spline") {   // C42: draggable through-points / control poles p0..pN
            for (size_t i = 0; i < e.pts.size(); ++i)
                consider(e.id + ".p" + std::to_string(i), e.pts[i].x, e.pts[i].y);
        }
    }
    // The sketch origin (0,0) is a real, pickable point handle so it can be selected and given
    // relations (coincident / horizontal / vertical / point_on …) against other geometry. It is a
    // virtual entity — not in ents_ — and the engine resolves the bare ref "origin" to a fixed (0,0).
    // Considered LAST so a real vertex sitting exactly on the origin wins the tie (strict d2<best);
    // the origin is then picked only when no geometry handle is nearer. It is fixed, so it is
    // selectable but never promotes to a drag (see the drag-promotion guard).
    consider("origin", 0.0f, 0.0f);
    return hit;
}

// Nearest whole entity under the screen point. Distance is measured to the projected geometry:
// segment for lines/slots, |dist-to-center - radius_px| for circles/arcs, and the same rim metric
// (no angular clamp) for arcs — a deliberately simple approximation matching the Smart-Dimension
// picker. Returns the ents_ index or -1 when nothing is within maxPx.

}  // namespace ui
