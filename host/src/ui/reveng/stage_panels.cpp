// Stage panels for the Reverse Engineering workspace center column (contracts/reveng.md).
// See stage_panels.h for the panel list. All worker calls are ASYNC st.rpc.call with
// GL-thread callbacks — never a sync call in a draw path (known freeze bug).
//
// RE-40 overhaul: every group is a ui::reveng::kit glass card, every size comes from
// ui::metrics::re*(), every colour comes from the kit / the theme palette, and every stage
// SHOWS the artifact it produced (scale mat photo, the picked two-point line, the fitted
// mesh, the built parametric body) instead of asserting that it worked.
#include "stage_panels.h"

#include <algorithm>
#include <cctype>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <string>
#include <utility>
#include <vector>

#include "imgui.h"
#include <nlohmann/json.hpp>

#include "core/app_state.h"
#include "ui_theme.h"
#include "../metrics.h"
#include "../file_dialog.h"   // saveFileDialog for the export-bundle destination

#include "reveng_internal.h"
#include "reveng_widgets.h"   // RE-40 shared visual kit
#include "img_tex.h"          // loadTexFromFile / freeTexture for the photo previews

namespace ui {
namespace reveng {

namespace M = ui::metrics;
using kit::Level;

// =================================================================================================
// shared helpers
// =================================================================================================

namespace {

// ---- vocabulary ---------------------------------------------------------------------------------

const char* const kScaleMethodIds[3]    = { "aruco_mat", "two_point", "known_dim" };
const char* const kScaleMethodLabels[3] = { "ArUco mat", "Two-point", "Known dimension" };
const char* const kScaleMethodHelp[3]   = {
    "Print the ArUco mat, lay it flat NEXT TO the part (same plane, same photos), and shoot the "
    "whole set. The solver triangulates the board's known square size out of the sparse "
    "reconstruction and derives mm-per-unit with no typing at all. Most accurate method — use it "
    "whenever you can print.",
    "Pick two pixels on one photo whose real separation you can measure with calipers, and type "
    "that distance. Only valid when both points lie in the same plane and roughly face the "
    "camera — perspective on a slanted feature will bias the whole model.",
    "Type one dimension you already trust (a caliper reading, a datasheet value). Everything "
    "else scales from it, so pick a feature that is long, flat-on to the camera, and easy to "
    "measure — a short or foreshortened one multiplies its own error into every other number.",
};

const char* const kPaperLabels[2]     = { "A4", "US Letter" };
const char* const kKnownSourceIds[4]  = { "caliper", "datasheet", "photo_fiducial", "guessed" };
const char* const kKnownSourceLabels[4] = { "Caliper", "Datasheet", "Photo fiducial", "Guessed" };
const char* const kStrategyLabels[2]  = { "Auto (fits + AI)", "Mesh-body fallback" };

// ---- small text helpers ------------------------------------------------------------------------

// Dim explanatory prose. Uses the style's disabled-text role, so no colour literal is involved.
void dimWrapped(const char* text) {
    if (!text || !text[0]) return;
    ImGui::PushStyleColor(ImGuiCol_Text, ImGui::GetStyleColorVec4(ImGuiCol_TextDisabled));
    ImGui::TextWrapped("%s", text);
    ImGui::PopStyleColor();
}

// Mirrors kit::drawChip's measurement so CardHeaderRight can right-align a chip exactly.
float chipWidth(const char* label, const char* value = nullptr) {
    const float padX = M::U(1.75f);
    const float dotW = M::U(0.5f) * 2.0f;
    float w = padX * 2.0f + dotW + M::U(1.25f) + ImGui::CalcTextSize(label ? label : "").x;
    if (value && value[0]) w += M::U(1.25f) + ImGui::CalcTextSize(value).x;
    return w;
}

std::string fmtNum(double v, const char* spec = "%.6g") {
    char b[64];
    std::snprintf(b, sizeof(b), spec, v);
    return std::string(b);
}

// Last path component, for captions that must stay readable in a narrow card.
std::string baseName(const std::string& path) {
    const size_t slash = path.find_last_of("/\\");
    return slash == std::string::npos ? path : path.substr(slash + 1);
}

// ---- verdict bands -------------------------------------------------------------------------------

// residual_pct arrives as a percentage number (0.4 == 0.4 %).
Level levelForResidual(double residualPct) {
    if (!(residualPct > 0.0)) return Level::Neutral;
    if (residualPct < 1.0)    return Level::Ok;
    if (residualPct < 3.0)    return Level::Warn;
    return Level::Error;
}

Level levelForConfidence(double c01) {
    if (c01 >= 0.80) return Level::Ok;
    if (c01 >= 0.50) return Level::Warn;
    return Level::Error;
}

// contracts/reveng.md verdict bands for describe_reveng_result.match_pct (0..100).
Level levelForMatch(double pct) {
    if (pct >= 98.0) return Level::Ok;
    if (pct >= 95.0) return Level::Warn;
    return Level::Error;
}
const char* matchVerdict(double pct) {
    if (pct >= 98.0) return "pass";
    if (pct >= 95.0) return "conditional";
    return "fail";
}

// The honesty tier a known_dim source earns (contracts/reveng.md §0 — no silent guessing).
const char* tierForKnownSource(int idx) {
    switch (idx) {
        case 0:  return "exact";        // caliper — the user measured it
        case 1:  return "high";         // datasheet
        case 2:  return "medium";       // photo_fiducial
        default: return "approximate";  // guessed — never dimensionally trustworthy
    }
}

// The honesty tier the session's current scale earns.
const char* tierForScale(const ScaleState& sc) {
    if (!sc.mirrored || !(sc.mmPerUnit > 0.0)) return "approximate";
    if (!sc.confirmed)                         return "low";
    if (sc.residualPct < 1.0)                  return "high";
    if (sc.residualPct < 3.0)                  return "medium";
    return "low";
}

// ---- capability mirror ----------------------------------------------------------------------------

const CapabilityState::Artifact* capArtifact(const char* key) {
    for (const CapabilityState::Artifact& a : S().caps.artifacts)
        if (a.key == key) return &a;
    return nullptr;
}

std::string capHint(const char* key, const char* fallback) {
    const CapabilityState::Artifact* a = capArtifact(key);
    if (a && !a->hint.empty()) return a->hint;
    return std::string(fallback);
}

// Reason string when the mesh-producing backend cannot serve this session, "" when it can.
// Never pre-blocks on an unanswered probe, and never blocks when a mesh already exists.
std::string meshBackendBlock() {
    const RevEngState& s = S();
    if (!s.artifactMeshPath.empty()) return {};
    if (!s.caps.loaded)              return {};
    if (s.caps.dense)                return {};
    return "This session has no mesh and the dense/mesh backend is not provisioned — " +
           capHint("openmvs", "set OPENMVS_BIN to your OpenMVS build (see reveng_status())") +
           ", then run Reconstruct.";
}

// ---- async unwrap ------------------------------------------------------------------------------

// Standard async-RPC result unwrap. Returns true on success; on failure writes a human message
// into errOut and the machine-readable contract fields into reasonOut / hintOut so a caller can
// branch on a specific failure (e.g. "scale_needs_sparse") instead of pattern-matching prose.
bool rpcUnwrap(const core::RpcResult& r, const char* tool,
               std::string& errOut, std::string& reasonOut, std::string& hintOut) {
    reasonOut.clear();
    hintOut.clear();
    if (!r.ok) {
        errOut = r.err.empty() ? std::string(tool) + " failed" : r.err;
        return false;
    }
    if (!r.result.is_object()) {
        errOut = std::string(tool) + " returned a malformed response";
        return false;
    }
    if (r.result.contains("ok") && !r.result.value("ok", true)) {
        reasonOut = r.result.value("reason", std::string());
        hintOut   = r.result.value("hint", std::string());
        errOut    = reasonOut.empty() ? (std::string(tool) + " failed") : reasonOut;
        if (!hintOut.empty()) errOut += " \xe2\x80\x94 " + hintOut;
        return false;
    }
    return true;
}

bool rpcOk(const core::RpcResult& r, const char* tool, std::string& errOut) {
    std::string reason, hint;
    return rpcUnwrap(r, tool, errOut, reason, hint);
}

// ---- lazily-loaded preview textures -----------------------------------------------------------

// One long-lived GL texture bound to a path. sync() is a no-op while the path is unchanged, so a
// draw path can call it every frame; a failed decode is remembered so we never retry per-frame.
struct TexSlot {
    std::string  path;
    unsigned int tex = 0;
    int          w = 0, h = 0;
    bool         failed = false;

    void sync(const std::string& want) {
        if (want == path) return;
        if (tex) { ::reveng::freeTexture(tex); tex = 0; }
        w = h = 0;
        failed = false;
        path = want;
        if (path.empty()) return;
        tex = ::reveng::loadTexFromFile(path, &w, &h);
        failed = (tex == 0);
    }
};

// Letterboxed image rect inside a kit::HeroPreview, given the rect the widget reserved.
// Mirrors HeroPreview's fit in reveng_widgets.cpp exactly (inset U(1), aspect-fit, centred);
// the caption strip is an overlay scrim and does NOT change the fit. Returns false when the
// slot has no usable image.
bool heroImageRect(const ImVec2& p0, const ImVec2& p1, int texW, int texH,
                   ImVec2& f0, ImVec2& f1) {
    if (texW <= 0 || texH <= 0) return false;
    const float inset = M::U(1.0f);
    const ImVec2 in0(p0.x + inset, p0.y + inset);
    const ImVec2 in1(p1.x - inset, p1.y - inset);
    const float bw = in1.x - in0.x;
    const float bh = in1.y - in0.y;
    if (!(bw > 0.0f) || !(bh > 0.0f)) return false;
    const float aspect = (float)texW / (float)texH;
    float dw = bw, dh = bw / aspect;
    if (dh > bh) { dh = bh; dw = bh * aspect; }
    f0 = ImVec2(in0.x + (bw - dw) * 0.5f, in0.y + (bh - dh) * 0.5f);
    f1 = ImVec2(f0.x + dw, f0.y + dh);
    return true;
}

// A preview box that respects the source aspect but stays inside the stage's vertical rhythm.
float heroBoxHeight(float boxW, int texW, int texH) {
    if (texW <= 0 || texH <= 0 || !(boxW > 0.0f)) return M::reThumbLg();
    const float h = boxW * (float)texH / (float)texW;
    return std::min(std::max(h, M::reThumbMd()), M::rePreviewMinW());
}

// =================================================================================================
// panel-local state
//
// SEAM: the shared structs in reveng_internal.h (FitEntry / ModelState / ScaleState /
// DeliverState) are owned by the workspace shell, so the RE-40 fields this file needs live here,
// file-static, keyed by session id — exactly the webcam_capture.cpp idiom.
// =================================================================================================

// ---- Scale ---------------------------------------------------------------------------------------
struct ScaleLocal {
    std::string sid;
    TexSlot     matTex;            // printed ArUco mat (DeliverState::matPath)
    TexSlot     srcTex;            // two-point source photo
    int         nextPoint = 0;     // 0 -> next click sets P1, 1 -> sets P2
    int         lastGallerySel = -1;
    std::string failReason;        // raw contract `reason` from the last reveng_set_scale failure
    std::string failHint;
    double      probeUnits = 10.0; // plausibility probe, in reconstruction units

    void sync(const std::string& s) {
        if (s == sid) return;
        sid = s;
        nextPoint = 0;
        lastGallerySel = -1;
        failReason.clear();
        failHint.clear();
        probeUnits = 10.0;
    }
};
ScaleLocal& SL() { static ScaleLocal v; return v; }

// ---- Model ----------------------------------------------------------------------------------------
// Richer parse of one aicad.reveng_fits/1 entry. FitEntry (reveng_internal.h) only carries
// kind/confidence/source/paramsSummary, so role / through / depth_mm / per-kind params live here,
// index-parallel to ModelState::fits.
struct FitDetail {
    std::string kind;
    std::string role;                  // cylinder: hole|boss|shaft|unknown; plane: pocket|pad|unknown
    bool        hasThrough  = false;
    bool        through     = false;
    bool        hasDepth    = false;
    double      depthMm     = 0.0;
    bool        hasResidual = false;
    double      residualMm  = 0.0;
    bool        hasInliers  = false;
    double      inliers     = 0.0;     // point count; double so the JSON read stays uniform
    double      maxMm       = 0.0;     // largest millimetre dimension this fit reported
    std::vector<std::pair<std::string, std::string>> rows;  // preformatted label -> value
};

// Result phases — "not built yet" and "built but unscored" are different states and must never
// collapse into the same blank dash.
enum class ResultPhase { Loading, NotBuilt, Failed, Built };

struct ModelLocal {
    std::string            sid;
    std::vector<FitDetail> details;
    std::string            fitsMeshPath;    // mesh_path echoed by reveng_fit_primitives
    // notes[] — the fitter's own honest degradation report ({stage, reason, hint}). Surfaced,
    // never swallowed: a fit that silently fell back is exactly what misleads an engineer.
    std::vector<std::string> notes;
    bool                   matchScored = false;  // describe_reveng_result gave a non-null match_pct
    std::string            descReason;           // raw contract reason on failure

    void sync(const std::string& s) {
        if (s == sid) return;
        sid = s;
        details.clear();
        notes.clear();
        fitsMeshPath.clear();
        matchScored = false;
        descReason.clear();
    }
};
ModelLocal& ML() { static ModelLocal v; return v; }

// ---- Deliver ----------------------------------------------------------------------------------------
struct DeliverLocal {
    std::string sid;
    TexSlot     matTex;
    std::string attachPath;   // geometry_ref returned by reveng_attach_result
    std::string exportPath;   // path returned by export_reveng_bundle

    void sync(const std::string& s) {
        if (s == sid) return;
        sid = s;
        attachPath.clear();
        exportPath.clear();
    }
};
DeliverLocal& DL() { static DeliverLocal v; return v; }

}  // namespace

// Selected session id, or "" when nothing valid is selected.
static std::string selectedSessionId() {
    const RevEngState& s = S();
    if (s.selected < 0 || s.selected >= (int)s.sessions.size()) return {};
    return s.sessions[s.selected].session_id;
}

// Name of the selected session (falls back to the id).
static std::string selectedSessionName() {
    const RevEngState& s = S();
    if (s.selected < 0 || s.selected >= (int)s.sessions.size()) return {};
    const SessionSummary& ss = s.sessions[(size_t)s.selected];
    return ss.name.empty() ? ss.session_id : ss.name;
}

static bool selectedSessionApproximate() {
    const RevEngState& s = S();
    if (s.selected < 0 || s.selected >= (int)s.sessions.size()) return false;
    return s.sessions[(size_t)s.selected].approximate;
}

// Panel-wide "you have not picked a session" state. Returns true when the panel should stop.
static bool guardNoSession(const char* what) {
    if (!selectedSessionId().empty()) return false;
    if (kit::EmptyState("part", "No session selected",
                        what,
                        "Go to Sources")) {
        S().stage = Stage::Source;
    }
    return true;
}

// =================================================================================================
// Scale stage
// =================================================================================================

static void kickSetScale(AppState& st) {
    RevEngState& s = S();
    ScaleState& sc = s.scale;
    const std::string sid = selectedSessionId();
    if (sc.pending || sid.empty()) return;
    sc.pending = true;
    sc.err.clear();
    sc.okMsg.clear();
    SL().failReason.clear();
    SL().failHint.clear();

    nlohmann::json params = nlohmann::json::object();
    if (sc.method == 0) {
        // The paper size lives on DeliverState (the Print control owns it) — mirror it onto the
        // scale state so the solve and the printed sheet can never disagree.
        sc.board = s.deliver.matSize;
        params["board"] = (sc.board == 1) ? "Letter" : "A4";
    } else if (sc.method == 1) {
        if (sc.imageIdx >= 0 && sc.imageIdx < (int)s.galleryImages.size())
            params["image"] = s.galleryImages[(size_t)sc.imageIdx].path;
        params["p1"] = { sc.p1[0], sc.p1[1] };
        params["p2"] = { sc.p2[0], sc.p2[1] };
        params["distance_mm"] = sc.distMm;
    } else {
        params["feature"]  = std::string(sc.featureBuf);
        params["value_mm"] = sc.knownValueMm;
        params["source"]   = kKnownSourceIds[std::clamp(sc.knownSource, 0, 3)];
    }

    nlohmann::json args = nlohmann::json::object();
    args["session_id"] = sid;
    args["method"]     = kScaleMethodIds[std::clamp(sc.method, 0, 2)];
    args["params"]     = params;

    st.rpc.call("reveng_set_scale", args, [sid](const core::RpcResult& r) {
        RevEngState& s2 = S();
        ScaleState& sc2 = s2.scale;
        sc2.pending = false;
        if (selectedSessionId() != sid) return;   // user moved on; drop the stale reply

        std::string reason, hint;
        if (!rpcUnwrap(r, "reveng_set_scale", sc2.err, reason, hint)) {
            SL().failReason = reason;
            SL().failHint   = hint;
            return;
        }
        sc2.err.clear();
        SL().failReason.clear();
        SL().failHint.clear();
        if (r.result.contains("scale") && r.result["scale"].is_object()) {
            const auto& sj = r.result["scale"];
            sc2.mirrored    = true;
            sc2.curMethod   = sj.value("method", std::string());
            sc2.mmPerUnit   = sj.value("mm_per_unit", 0.0);
            sc2.residualPct = sj.value("residual_pct", 0.0);
            sc2.confirmed   = sj.value("confirmed", false);
        }
        sc2.okMsg = sc2.confirmed
            ? "Scale confirmed — reconstruction is unblocked."
            : "Scale recorded, but not yet confirmed — reconstruction stays gated until it is.";
    });
}

// reveng_print_scale_mat (shared by the Scale aruco card, the Deliver panel, and the ribbon).
void kickPrintScaleMat(AppState& st) {
    RevEngState& s = S();
    DeliverState& d = s.deliver;
    if (d.matPending) return;
    d.matPending = true;
    d.matErr.clear();
    d.matPath.clear();
    nlohmann::json args = nlohmann::json::object();
    args["size"] = (d.matSize == 1) ? "Letter" : "A4";
    st.rpc.call("reveng_print_scale_mat", args, [&st](const core::RpcResult& r) {
        RevEngState& s2 = S();
        DeliverState& d2 = s2.deliver;
        d2.matPending = false;
        if (!rpcOk(r, "reveng_print_scale_mat", d2.matErr)) {
            st.toast(ui::ToastLevel::Error, d2.matErr);
            return;
        }
        d2.matErr.clear();
        d2.matPath = r.result.value("path", std::string());
        st.toast(ui::ToastLevel::Success,
                 d2.matPath.empty() ? std::string("Scale mat printed")
                                    : ("Scale mat written: " + d2.matPath));
    });
}

// The ArUco scale-mat card: hero preview of the printed sheet + paper size + print action.
// Shared verbatim by the Scale stage (method = ArUco) and the Deliver stage.
static void drawScaleMatCard(AppState& st, TexSlot& tex, const char* cardId) {
    DeliverState& d = S().deliver;
    tex.sync(d.matPath);

    if (!kit::CardBegin(cardId, "ArUco scale mat", "measure")) { kit::CardEnd(); return; }

    const char* stateLabel = d.matPending ? "printing"
                           : !d.matErr.empty() ? "failed"
                           : !d.matPath.empty() ? "written" : "not printed";
    const Level stateLevel = d.matPending ? Level::Busy
                           : !d.matErr.empty() ? Level::Error
                           : !d.matPath.empty() ? Level::Ok : Level::Neutral;
    kit::CardHeaderRight(chipWidth(stateLabel));
    kit::StatusChip(stateLabel, stateLevel,
                    "reveng_print_scale_mat writes a PDF/PNG of the calibration board. Print it, "
                    "then lay it flat next to the part in every photo.");

    const float boxW = ImGui::GetContentRegionAvail().x;
    const std::string caption = d.matPath.empty() ? std::string() : baseName(d.matPath);
    kit::HeroPreview("##re_mat_hero", tex.tex, tex.w, tex.h,
                     ImVec2(boxW, heroBoxHeight(boxW, tex.w, tex.h)),
                     caption.empty() ? nullptr : caption.c_str(),
                     tex.failed ? "Mat written, but the preview could not be decoded"
                                : "Print a scale mat to preview it here");

    kit::GapControl();
    kit::FieldLabel("Paper", "Pick the sheet your printer actually holds — the square size baked "
                             "into the board depends on it.");
    kit::SegmentedControl("##re_mat_size", kPaperLabels, 2, &d.matSize, M::reFieldW());

    kit::GapControl();
    if (kit::PrimaryButton(d.matPending ? "Printing\xe2\x80\xa6" : "Print scale mat",
                           !d.matPending, "A scale mat is already being written.")) {
        kickPrintScaleMat(st);
    }
    if (!d.matPath.empty()) {
        ImGui::SameLine(0, M::reCtlGap());
        if (kit::SecondaryButton("Copy path")) ImGui::SetClipboardText(d.matPath.c_str());
    }

    if (!d.matPath.empty()) {
        kit::GapControl();
        kit::KeyValueRow("Wrote", d.matPath.c_str(), Level::Ok);
    }
    kit::InlineStatus(d.matErr.c_str(), Level::Error);
    kit::CardEnd();
}

// The "print at 100 %" warning. Its own card because "fit to page" is the single most common way
// this whole feature silently produces a part that does not fit.
static void drawMatScaleWarningCard() {
    if (!kit::CardBegin("re_mat_warn", "Print at 100 % — actual size", "warning_badge")) {
        kit::CardEnd();
        return;
    }
    kit::CardHeaderRight(chipWidth("critical"));
    kit::StatusChip("critical", Level::Error,
                    "The mat IS the physical reference. If it prints at the wrong size, every "
                    "dimension downstream is wrong by the same factor and nothing in the pipeline "
                    "can detect it.");
    kit::InlineStatus(
        "In the print dialog set Scale to 100 % (Actual size). \"Fit to page\", \"Shrink oversized "
        "pages\" and borderless modes silently resize the board.", Level::Error);
    kit::GapControl();
    dimWrapped("After printing, measure one square with calipers and confirm it matches the size "
               "printed on the sheet before you shoot a single photo. Tape the mat flat — a curled "
               "sheet biases the solve just as badly as a mis-scaled one.");
    kit::CardEnd();
}

// Two-point picker: source photo, click-to-pick overlay, numeric fallback, live mm/px readout.
static void drawTwoPointCard() {
    RevEngState& s = S();
    ScaleState& sc = s.scale;
    ScaleLocal& L = SL();

    if (!kit::CardBegin("re_scale_2pt", "Two-point reference", "smart_dim")) {
        kit::CardEnd();
        return;
    }

    if (s.galleryImages.empty()) {
        if (kit::EmptyState("open", "No photos in this session",
                            "Two-point scaling measures between two pixels on one photo. "
                            "Add photos in the Sources stage first.",
                            "Go to Sources")) {
            s.stage = Stage::Source;
        }
        kit::CardEnd();
        return;
    }

    // Two-way sync with the workspace-wide contact-sheet selection.
    const int nImg = (int)s.galleryImages.size();
    if (s.selectedImage != L.lastGallerySel) {
        L.lastGallerySel = s.selectedImage;
        if (s.selectedImage >= 0 && s.selectedImage < nImg) sc.imageIdx = s.selectedImage;
    }
    sc.imageIdx = std::clamp(sc.imageIdx, 0, nImg - 1);

    kit::FieldLabel("Source photo", "Pick the photo whose two points you can measure in the real "
                                    "world. A flat, camera-facing feature gives the least error.");
    ImGui::SetNextItemWidth(ImGui::GetContentRegionAvail().x);
    const std::string curName = baseName(s.galleryImages[(size_t)sc.imageIdx].path);
    if (ImGui::BeginCombo("##re_2pt_img", curName.c_str())) {
        for (int i = 0; i < nImg; ++i) {
            const bool sel = (i == sc.imageIdx);
            ImGui::PushID(i);
            if (ImGui::Selectable(baseName(s.galleryImages[(size_t)i].path).c_str(), sel)) {
                sc.imageIdx = i;
                s.selectedImage = i;
                L.lastGallerySel = i;
            }
            ImGui::PopID();
            if (sel) ImGui::SetItemDefaultFocus();
        }
        ImGui::EndCombo();
    }

    L.srcTex.sync(s.galleryImages[(size_t)sc.imageIdx].path);

    kit::GapControl();
    ImGui::TextUnformatted(L.nextPoint == 0 ? "Click the photo to set P1"
                                            : "Click the photo to set P2");
    ImGui::SameLine(0, M::reCtlGap());
    kit::StatusChip(L.nextPoint == 0 ? "next: P1" : "next: P2", Level::Busy,
                    "Clicks alternate between the two points. The numeric fields below stay in "
                    "sync either way — type them if you already know the pixel coordinates.");
    ImGui::SameLine(0, M::reCtlGap());
    if (kit::SecondaryButton("Reset points")) {
        sc.p1[0] = sc.p1[1] = 0.0f;
        sc.p2[0] = sc.p2[1] = 0.0f;
        L.nextPoint = 0;
    }

    // ---- hero + picking overlay ----
    const float boxW = ImGui::GetContentRegionAvail().x;
    const float boxH = heroBoxHeight(boxW, L.srcTex.w, L.srcTex.h);
    const std::string caption = baseName(L.srcTex.path);
    const bool clicked = kit::HeroPreview("##re_2pt_hero", L.srcTex.tex, L.srcTex.w, L.srcTex.h,
                                          ImVec2(boxW, boxH),
                                          caption.empty() ? nullptr : caption.c_str(),
                                          L.srcTex.failed ? "Photo could not be decoded"
                                                          : "Select a photo to pick points on");
    ImVec2 f0, f1;
    if (heroImageRect(ImGui::GetItemRectMin(), ImGui::GetItemRectMax(),
                      L.srcTex.w, L.srcTex.h, f0, f1) && L.srcTex.tex != 0u) {
        const float sx = (f1.x - f0.x) / (float)L.srcTex.w;
        const float sy = (f1.y - f0.y) / (float)L.srcTex.h;

        if (clicked) {
            const ImVec2 m = ImGui::GetIO().MousePos;
            if (m.x >= f0.x && m.x <= f1.x && m.y >= f0.y && m.y <= f1.y) {
                const float ix = std::clamp((m.x - f0.x) / sx, 0.0f, (float)(L.srcTex.w - 1));
                const float iy = std::clamp((m.y - f0.y) / sy, 0.0f, (float)(L.srcTex.h - 1));
                if (L.nextPoint == 0) { sc.p1[0] = ix; sc.p1[1] = iy; L.nextPoint = 1; }
                else                  { sc.p2[0] = ix; sc.p2[1] = iy; L.nextPoint = 0; }
            }
        }

        ImDrawList* dl = ImGui::GetWindowDrawList();
        dl->PushClipRect(f0, f1, true);
        const ImVec2 a(f0.x + sc.p1[0] * sx, f0.y + sc.p1[1] * sy);
        const ImVec2 b(f0.x + sc.p2[0] * sx, f0.y + sc.p2[1] * sy);
        const ImU32 line  = kit::levelColor(Level::Ok);
        const ImU32 halo  = kit::levelColor(Level::Neutral);
        const float r      = M::U(1.25f);
        const float haloW  = M::stroke(3.0f);
        const float lineW  = M::stroke(1.5f);
        const float ringW  = M::stroke(1.0f);

        dl->AddLine(a, b, halo, haloW);
        dl->AddLine(a, b, line, lineW);
        for (int i = 0; i < 2; ++i) {
            const ImVec2 c = i == 0 ? a : b;
            dl->AddCircleFilled(c, r + ringW, halo);
            dl->AddCircleFilled(c, r, line);
            const char* lbl = i == 0 ? "1" : "2";
            const ImVec2 ts = ImGui::CalcTextSize(lbl);
            dl->AddText(ImVec2(c.x - ts.x * 0.5f, c.y - ts.y - r - M::U(0.5f)), line, lbl);
        }
        dl->PopClipRect();
    }

    // ---- numeric fallback (synced both ways with the picker) ----
    kit::GapControl();
    kit::FieldLabel("P1 (px)", "Pixel coordinates of the first point. Clicking the photo writes "
                               "these; typing them moves the marker.");
    const float pxMax = (float)std::max(1, std::max(L.srcTex.w, L.srcTex.h));
    ImGui::DragFloat2("##re_2pt_p1", sc.p1, 1.0f, 0.0f, pxMax, "%.0f");
    kit::FieldLabel("P2 (px)");
    ImGui::DragFloat2("##re_2pt_p2", sc.p2, 1.0f, 0.0f, pxMax, "%.0f");
    kit::FieldLabel("Real distance", "Measure this span on the physical part with calipers.");
    ImGui::InputDouble("##re_2pt_dist", &sc.distMm, 0.0, 0.0, "%.3f mm");
    if (sc.distMm <= 0.0) sc.distMm = 0.001;

    // ---- live cross-check straight from the user's own numbers ----
    const double dx = (double)sc.p2[0] - (double)sc.p1[0];
    const double dy = (double)sc.p2[1] - (double)sc.p1[1];
    const double dPx = std::sqrt(dx * dx + dy * dy);
    kit::GapSection();
    kit::SectionHeader("Sanity check", "check");
    if (dPx < 1.0) {
        kit::InlineStatus("Pick two different points — the span is under one pixel, which cannot "
                          "produce a scale.", Level::Warn);
    } else {
        const double mmPerPx = sc.distMm / dPx;
        kit::KeyValueRow("Span", (fmtNum(dPx, "%.0f") + " px").c_str());
        kit::KeyValueRow("Scale on this photo", (fmtNum(mmPerPx, "%.5g") + " mm/px").c_str());
        if (L.srcTex.w > 0) {
            kit::KeyValueRow("Whole frame spans",
                             kit::fmtMm(mmPerPx * (double)L.srcTex.w, 1).c_str());
        }
        dimWrapped("If the frame width above is nowhere near the real width of the scene you "
                   "photographed, the distance or the points are wrong — fix them before applying.");
    }
    kit::CardEnd();
}

static void drawKnownDimCard() {
    ScaleState& sc = S().scale;

    if (!kit::CardBegin("re_scale_known", "Known dimension", "smart_dim")) {
        kit::CardEnd();
        return;
    }

    kit::FieldLabel("Feature", "Name the feature this dimension belongs to, e.g. "
                               "output_shaft.dia. It is recorded in the provenance chain.");
    ImGui::SetNextItemWidth(ImGui::GetContentRegionAvail().x);
    ImGui::InputTextWithHint("##re_kd_feat", "output_shaft.dia",
                             sc.featureBuf, sizeof(sc.featureBuf));

    kit::FieldLabel("Value", "The real, physical size of that feature in millimetres.");
    ImGui::InputDouble("##re_kd_val", &sc.knownValueMm, 0.0, 0.0, "%.4g mm");
    if (sc.knownValueMm <= 0.0) sc.knownValueMm = 0.001;

    kit::FieldLabel("Source", "Where the number came from. This drives the honesty tier that "
                              "rides along with every dimension derived from it.");
    kit::SegmentedControl("##re_kd_src", kKnownSourceLabels, 4, &sc.knownSource,
                          ImGui::GetContentRegionAvail().x);

    kit::GapControl();
    const int srcIdx = std::clamp(sc.knownSource, 0, 3);
    kit::HonestyTag(tierForKnownSource(srcIdx),
                    (std::string("source: ") + kKnownSourceIds[srcIdx]).c_str());
    ImGui::SameLine(0, M::reCtlGap());
    kit::ValueChip("value", kit::fmtMm(sc.knownValueMm, 3).c_str(), Level::Neutral);

    if (srcIdx == 3) {
        kit::GapControl();
        kit::InlineStatus(
            "Guessed. contracts/reveng.md \xc2\xa7""0 forbids silent guessing on fit-critical "
            "dimensions: everything scaled from this number is reference-only and MUST be "
            "confirmed with calipers before anything is cut, printed or ordered.", Level::Error);
    }
    kit::CardEnd();
}

// The scale of record + the plausibility readout. Always rendered, even with no scale set.
static void drawScaleResultCard() {
    RevEngState& s = S();
    ScaleState& sc = s.scale;
    ScaleLocal& L = SL();

    if (!kit::CardBegin("re_scale_state", "Scale of record", "measure")) {
        kit::CardEnd();
        return;
    }

    const char* confLabel = sc.confirmed ? "confirmed" : "unconfirmed";
    kit::CardHeaderRight(chipWidth(confLabel));
    kit::StatusChip(confLabel, sc.confirmed ? Level::Ok : Level::Warn,
                    sc.confirmed
                        ? "Scale is confirmed — the reconstruct and model stages are unblocked."
                        : "Reconstruction stays gated until the scale is confirmed.");

    if (!sc.mirrored || !(sc.mmPerUnit > 0.0)) {
        kit::EmptyState("smart_dim", "No scale set yet",
                        "Every downstream dimension is multiplied by this one number. A 3 % error "
                        "here prints a part that does not fit, and nothing later in the pipeline "
                        "can detect it.\nPick a method below and apply it.");
        kit::CardEnd();
        return;
    }

    kit::ValueChip("method", sc.curMethod.empty() ? "(unknown)" : sc.curMethod.c_str(),
                   Level::Neutral);
    ImGui::SameLine(0, M::reCtlGap());
    kit::ValueChip("mm / unit", fmtNum(sc.mmPerUnit).c_str(), Level::Neutral,
                   "Multiply any length measured in the reconstruction by this to get "
                   "millimetres.");
    ImGui::SameLine(0, M::reCtlGap());
    kit::ValueChip("residual", kit::fmtPct(sc.residualPct / 100.0, 2).c_str(),
                   levelForResidual(sc.residualPct),
                   "Solver fit error. Under 1 % is good, 1-3 % is usable with care, above 3 % "
                   "means the solve did not agree with itself — re-shoot or re-pick.");
    ImGui::SameLine(0, M::reCtlGap());
    kit::HonestyTag(tierForScale(sc), "Tier of every dimension derived through this scale.");

    kit::GapSection();
    kit::SectionHeader("Plausibility check", "check");
    dimWrapped("Before you confirm, eyeball the conversion. If a length you can see in the "
               "reconstruction comes out at an implausible size, the scale is wrong — not the "
               "part.");

    kit::FieldLabel("Probe length", "A length you can see in the reconstruction, in reconstruction "
                                    "units. The row below converts it to millimetres.");
    ImGui::InputDouble("##re_scale_probe", &L.probeUnits, 0.0, 0.0, "%.4g units");
    if (!(L.probeUnits > 0.0)) L.probeUnits = 1.0;

    kit::KeyValueRow((fmtNum(L.probeUnits, "%.4g") + " units").c_str(),
                     kit::fmtMm(L.probeUnits * sc.mmPerUnit, 2).c_str(), Level::Ok);
    kit::KeyValueRow("1 unit",   kit::fmtMm(sc.mmPerUnit, 4).c_str());
    kit::KeyValueRow("10 units", kit::fmtMm(10.0 * sc.mmPerUnit, 2).c_str());
    kit::KeyValueRow("100 units", kit::fmtMm(100.0 * sc.mmPerUnit, 1).c_str());

    // The part's own geometry, measured through this very scale — the strongest eyeball check
    // available, because the number is the real part's size in millimetres.
    double partMax = 0.0;
    for (const FitDetail& fd : ML().details) partMax = std::max(partMax, fd.maxMm);
    if (partMax > 0.0) {
        kit::GapControl();
        kit::KeyValueRow("Largest fitted feature", kit::fmtMm(partMax, 2).c_str(),
                         Level::Ok);
        dimWrapped("Measured on the fitted primitives at the current scale. If that is not "
                   "roughly the size of the part in your hand, the scale is wrong — fix it here "
                   "before anything downstream inherits the error.");
    } else {
        kit::GapControl();
        dimWrapped("Run Fit primitives in the Model stage and this card will also show the "
                   "largest feature it found, in millimetres, so you can compare it against the "
                   "real part.");
    }
    kit::CardEnd();
}

void drawScalePanel(AppState& st) {
    RevEngState& s = S();
    ScaleState& sc = s.scale;
    const std::string sid = selectedSessionId();
    ScaleLocal& L = SL();
    L.sync(sid);
    ML().sync(sid);   // the plausibility readout reads the fitted extents

    kit::SectionHeader("Scale \xe2\x80\x94 give the reconstruction a physical size", "measure");
    if (guardNoSession("Scale belongs to a session. Pick one in the rail, or start a capture in "
                       "the Sources stage.")) {
        return;
    }

    drawScaleResultCard();
    kit::GapCards();

    // ---- method chooser ----
    if (kit::CardBegin("re_scale_method", "How do you want to set scale?", "smart_dim")) {
        sc.method = std::clamp(sc.method, 0, 2);
        kit::SegmentedControl("##re_scale_method", kScaleMethodLabels, 3, &sc.method,
                              ImGui::GetContentRegionAvail().x);
        kit::GapControl();
        // Wrap-safe legend: three "label (?)" pairs that fall to a new line instead of clipping
        // off the right edge of a narrow card.
        const float legendW = ImGui::GetContentRegionAvail().x;
        const float markerW = ImGui::CalcTextSize("(?)").x;
        float rowX = 0.0f;
        for (int i = 0; i < 3; ++i) {
            const float itemW =
                ImGui::CalcTextSize(kScaleMethodLabels[i]).x + M::reLabelGap() + markerW;
            if (i > 0) {
                if (rowX + M::reSectionGap() + itemW <= legendW) {
                    ImGui::SameLine(0, M::reSectionGap());
                    rowX += M::reSectionGap();
                } else {
                    rowX = 0.0f;
                }
            }
            ImGui::PushStyleColor(ImGuiCol_Text, ImGui::GetStyleColorVec4(ImGuiCol_TextDisabled));
            ImGui::TextUnformatted(kScaleMethodLabels[i]);
            ImGui::PopStyleColor();
            ImGui::SameLine(0, M::reLabelGap());
            kit::HelpMarker(kScaleMethodHelp[i]);
            rowX += itemW;
        }
    }
    kit::CardEnd();
    kit::GapCards();

    // ---- method body ----
    if (sc.method == 0) {
        drawScaleMatCard(st, L.matTex, "re_scale_mat");
        kit::GapCards();
        drawMatScaleWarningCard();
    } else if (sc.method == 1) {
        drawTwoPointCard();
    } else {
        drawKnownDimCard();
    }
    kit::GapCards();

    // ---- apply ----
    if (kit::CardBegin("re_scale_apply", "Apply scale", "check")) {
        const char* stateLabel = sc.pending ? "solving"
                               : !sc.err.empty() ? "failed"
                               : sc.mirrored ? "applied" : "idle";
        const Level stateLevel = sc.pending ? Level::Busy
                               : !sc.err.empty() ? Level::Error
                               : sc.mirrored ? Level::Ok : Level::Neutral;
        kit::CardHeaderRight(chipWidth(stateLabel));
        kit::StatusChip(stateLabel, stateLevel);

        const char* blocked = nullptr;
        if (sc.pending) {
            blocked = "A scale solve is already running.";
        } else if (sc.method == 1) {
            if (s.galleryImages.empty()) {
                blocked = "This session has no photos — add some in the Sources stage.";
            } else {
                const double dx = (double)sc.p2[0] - (double)sc.p1[0];
                const double dy = (double)sc.p2[1] - (double)sc.p1[1];
                if (std::sqrt(dx * dx + dy * dy) < 1.0)
                    blocked = "Pick two different points on the photo first.";
                else if (!(sc.distMm > 0.0))
                    blocked = "Type the real distance between the two points.";
            }
        } else if (sc.method == 2) {
            if (sc.featureBuf[0] == '\0')
                blocked = "Name the feature this dimension belongs to.";
            else if (!(sc.knownValueMm > 0.0))
                blocked = "Enter a positive value in millimetres.";
        }

        if (kit::PrimaryButton(sc.pending ? "Applying\xe2\x80\xa6" : "Apply scale",
                               blocked == nullptr, blocked)) {
            kickSetScale(st);
        }

        kit::GapControl();
        if (L.failReason == "scale_needs_sparse") {
            // A real, specific, recoverable failure — not a red string.
            if (kit::BlockedState(
                    "The ArUco solver needs a sparse reconstruction",
                    L.failHint.empty()
                        ? "Triangulating the mat's corners into millimetres requires the sparse "
                          "point cloud, and this session does not have one yet."
                        : L.failHint.c_str(),
                    "Run Reconstruct — sparse alone is enough, you do not need dense or a mesh — "
                    "then come back and apply the ArUco scale again.",
                    "Go to Reconstruct")) {
                L.failReason.clear();
                L.failHint.clear();
                s.stage = Stage::Reconstruct;
            }
        } else {
            kit::InlineStatus(sc.err.c_str(), Level::Error);
            kit::InlineStatus(sc.okMsg.c_str(), sc.confirmed ? Level::Ok : Level::Warn);
        }
    }
    kit::CardEnd();
}

// =================================================================================================
// Model stage
// =================================================================================================

// Preformatted "r=4.0 h=12.0" summary kept on FitEntry for the shell's properties column.
static std::string summarizeParams(const nlohmann::json& params) {
    if (!params.is_object()) return {};
    std::string out;
    char buf[64];
    for (auto it = params.begin(); it != params.end(); ++it) {
        if (it.value().is_number()) {
            std::snprintf(buf, sizeof(buf), "%s=%.4g",
                          it.key().c_str(), it.value().get<double>());
        } else if (it.value().is_array() && it.key() != "face_ids") {
            std::string joined;
            for (const auto& v : it.value()) {
                if (!v.is_number()) { joined.clear(); break; }
                if (!joined.empty()) joined += ",";
                char nb[24];
                std::snprintf(nb, sizeof(nb), "%.3g", v.get<double>());
                joined += nb;
            }
            if (joined.empty()) continue;
            std::snprintf(buf, sizeof(buf), "%s=[%s]", it.key().c_str(), joined.c_str());
        } else {
            continue;
        }
        if (!out.empty()) out += "  ";
        out += buf;
        if (out.size() > 90) { out += "\xe2\x80\xa6"; break; }
    }
    return out;
}

namespace {

// Keys a kind-specific pass already rendered, so the generic sweep does not duplicate them.
bool consumed(const std::vector<std::string>& used, const std::string& k) {
    for (const std::string& u : used) if (u == k) return true;
    return false;
}

bool jsonNum(const nlohmann::json& o, const char* key, double& out) {
    const auto it = o.find(key);
    if (it == o.end() || !it->is_number()) return false;
    out = it->get<double>();
    return true;
}

std::string jsonVec(const nlohmann::json& o, const char* key) {
    const auto it = o.find(key);
    if (it == o.end() || !it->is_array() || it->empty()) return {};
    std::string s = "[";
    for (size_t i = 0; i < it->size(); ++i) {
        if (!(*it)[i].is_number()) return {};
        char b[32];
        std::snprintf(b, sizeof(b), "%s%.3f", i ? ", " : "", (*it)[i].get<double>());
        s += b;
    }
    return s + "]";
}

constexpr double kPi = 3.14159265358979323846;

// Parse one fits[] entry into the rich display form (aicad.reveng_fits/1, fits_revision 2).
// role / through / depth_mm are TOP-LEVEL on the fit; params_mm carries the geometry. The
// lookup falls back to params_mm so an older revision still renders instead of going blank.
FitDetail parseFitDetail(const nlohmann::json& f) {
    FitDetail d;
    d.kind = f.value("kind", std::string("unknown"));
    if (d.kind.empty()) d.kind = "unknown";

    static const nlohmann::json kEmpty = nlohmann::json::object();
    const nlohmann::json& p =
        (f.contains("params_mm") && f["params_mm"].is_object()) ? f["params_mm"] : kEmpty;

    auto pick = [&](const char* key) -> const nlohmann::json* {
        auto it = f.find(key);
        if (it != f.end() && !it->is_null()) return &(*it);
        it = p.find(key);
        if (it != p.end() && !it->is_null()) return &(*it);
        return nullptr;
    };

    if (const nlohmann::json* j = pick("role"))
        if (j->is_string()) d.role = j->get<std::string>();
    if (const nlohmann::json* j = pick("through"))
        if (j->is_boolean()) { d.hasThrough = true; d.through = j->get<bool>(); }
    if (const nlohmann::json* j = pick("depth_mm"))
        if (j->is_number()) { d.hasDepth = true; d.depthMm = j->get<double>(); }
    if (const nlohmann::json* j = pick("residual_mm"))
        if (j->is_number()) { d.hasResidual = true; d.residualMm = j->get<double>(); }
    if (const nlohmann::json* j = pick("inliers"))
        if (j->is_number()) { d.hasInliers = true; d.inliers = j->get<double>(); }

    std::vector<std::string> used = { "face_ids" };
    std::vector<std::string> vectorKeys;   // positions / directions, rendered after the sizes
    double v = 0.0;

    auto rowMm = [&](const char* label, double mm, int dec = 2) {
        d.rows.emplace_back(label, kit::fmtMm(mm, dec));
        if (mm > d.maxMm) d.maxMm = mm;
    };
    auto takeNum = [&](const char* key, double& out) {
        used.emplace_back(key);
        return jsonNum(p, key, out);
    };
    auto takeVec = [&](const char* key) {
        used.emplace_back(key);
        if (!jsonVec(p, key).empty()) vectorKeys.emplace_back(key);
    };
    auto takeBool = [&](const char* key, const char* label) {
        used.emplace_back(key);
        const auto it = p.find(key);
        if (it != p.end() && it->is_boolean())
            d.rows.emplace_back(label, it->get<bool>() ? "yes" : "no");
    };

    if (d.kind == "cylinder") {
        // Engineers size holes and shafts by diameter — lead with it.
        if (takeNum("radius", v)) { rowMm("diameter", v * 2.0); rowMm("radius", v); }
        if (takeNum("height", v))   rowMm("height", v);
        takeVec("axis"); takeVec("base"); takeVec("origin");
    } else if (d.kind == "sphere") {
        if (takeNum("radius", v)) { rowMm("diameter", v * 2.0); rowMm("radius", v); }
        takeVec("center"); takeVec("origin");
    } else if (d.kind == "cone") {
        if (takeNum("radius_at_base", v)) {
            rowMm("base \xc3\xb8", v * 2.0);
            rowMm("base radius", v);
        }
        if (takeNum("height", v)) rowMm("height", v);
        if (takeNum("half_angle_rad", v)) {
            d.rows.emplace_back("half angle",
                                fmtNum(v * 180.0 / kPi, "%.2f") + "\xc2\xb0");
        }
        takeVec("apex"); takeVec("axis"); takeVec("origin");
    } else if (d.kind == "plane") {
        if (takeNum("offset", v)) rowMm("offset", v);
        takeVec("normal"); takeVec("origin");
    } else if (d.kind == "box") {
        double dxv = 0.0, dyv = 0.0, dzv = 0.0;
        used.emplace_back("dx"); used.emplace_back("dy"); used.emplace_back("dz");
        if (jsonNum(p, "dx", dxv) && jsonNum(p, "dy", dyv) && jsonNum(p, "dz", dzv)) {
            d.rows.emplace_back("size", kit::fmtMm(dxv, 2) + " \xc3\x97 " + kit::fmtMm(dyv, 2) +
                                        " \xc3\x97 " + kit::fmtMm(dzv, 2));
        }
        takeVec("origin");
    } else if (d.kind == "cross_section" || d.kind == "section") {
        if (takeNum("station_mm", v)) rowMm("station", v);
        if (takeNum("area_mm2", v))
            d.rows.emplace_back("area", fmtNum(v, "%.2f") + " mm\xc2\xb2");
        if (takeNum("perimeter_mm", v)) rowMm("perimeter", v);
        if (takeNum("loops", v)) d.rows.emplace_back("loops", fmtNum(v, "%.0f"));
        takeBool("closed", "closed");
        takeVec("axis");
    }

    // Generic sweep for anything the kind-specific pass did not claim. Units are inferred from
    // the key suffix so a radian or a count never renders as millimetres.
    for (auto it = p.begin(); it != p.end(); ++it) {
        const std::string& k = it.key();
        if (consumed(used, k)) continue;
        if (it.value().is_boolean()) {
            d.rows.emplace_back(k, it.value().get<bool>() ? "yes" : "no");
        } else if (it.value().is_number()) {
            const double n = it.value().get<double>();
            const bool rad  = k.size() > 4 && k.compare(k.size() - 4, 4, "_rad") == 0;
            const bool area = k.size() > 4 && k.compare(k.size() - 4, 4, "_mm2") == 0;
            const bool cnt  = k == "inliers" || k == "count" || k == "loops" ||
                              k.rfind("n_", 0) == 0;
            if (rad)             d.rows.emplace_back(k.substr(0, k.size() - 4),
                                                     fmtNum(n * 180.0 / kPi, "%.2f") + "\xc2\xb0");
            else if (area)       d.rows.emplace_back(k, fmtNum(n, "%.2f") + " mm\xc2\xb2");
            else if (cnt)        d.rows.emplace_back(k, fmtNum(n, "%.0f"));
            else if (k == "confidence") d.rows.emplace_back(k, kit::fmtPct(n, 0));
            else                 d.rows.emplace_back(k, kit::fmtMm(n, 3));
        } else if (it.value().is_array()) {
            const std::string vec = jsonVec(p, k.c_str());
            if (!vec.empty()) d.rows.emplace_back(k, vec);
        } else if (it.value().is_string()) {
            d.rows.emplace_back(k, it.value().get<std::string>());
        }
    }

    for (const std::string& k : vectorKeys) {
        const std::string vec = jsonVec(p, k.c_str());
        if (!vec.empty()) d.rows.emplace_back(k, vec);
    }
    return d;
}

// Fold one sections[] element (a TOP-LEVEL sibling of fits[], with its own shape) into the fit
// display form so cross-sections share the list, the group headers and the index space.
// Perimeter and loop count are derived here from the emitted polylines — the fitter does not
// send them, and they are the two numbers that make a slice legible.
nlohmann::json sectionToFit(const nlohmann::json& sec) {
    nlohmann::json out = nlohmann::json::object();
    out["kind"]   = "cross_section";
    out["source"] = sec.value("source", std::string("slice"));

    nlohmann::json pm = nlohmann::json::object();
    for (const char* k : { "station_mm", "area_mm2", "axis", "closed" })
        if (sec.contains(k) && !sec[k].is_null()) pm[k] = sec[k];

    int loops = 0;
    double perim = 0.0;
    if (sec.contains("polylines") && sec["polylines"].is_array()) {
        for (const auto& loop : sec["polylines"]) {
            if (!loop.is_array() || loop.size() < 2) continue;
            ++loops;
            for (size_t i = 1; i < loop.size(); ++i) {
                const auto& a = loop[i - 1];
                const auto& b = loop[i];
                if (!a.is_array() || !b.is_array() || a.size() < 2 || b.size() < 2) continue;
                if (!a[0].is_number() || !a[1].is_number() ||
                    !b[0].is_number() || !b[1].is_number()) continue;
                const double ddx = b[0].get<double>() - a[0].get<double>();
                const double ddy = b[1].get<double>() - a[1].get<double>();
                perim += std::sqrt(ddx * ddx + ddy * ddy);
            }
        }
    }
    pm["loops"] = loops;
    if (perim > 0.0) pm["perimeter_mm"] = perim;

    out["params_mm"] = pm;
    return out;
}

const char* iconForKind(const std::string& kind, const std::string& role) {
    if (kind == "cylinder")       return role == "hole" ? "hole_wizard" : "circle";
    if (kind == "box")            return "extrude_boss";
    if (kind == "plane")          return role == "pocket" ? "extrude_cut"
                                       : (role == "pad" ? "extrude_boss" : "plane");
    if (kind == "sphere")         return "circle";
    if (kind == "cone")           return "draft";
    if (kind == "cross_section" || kind == "section") return "section_view";
    return "geometry_analysis";
}

const char* pluralKind(const std::string& kind) {
    if (kind == "cylinder")       return "Cylinders";
    if (kind == "box")            return "Boxes";
    if (kind == "plane")          return "Planes";
    if (kind == "sphere")         return "Spheres";
    if (kind == "cone")           return "Cones";
    if (kind == "cross_section" || kind == "section") return "Cross-sections";
    return "Other primitives";
}

// Fixed display order so the list does not reshuffle between runs.
int kindRank(const std::string& kind) {
    if (kind == "cylinder")       return 0;
    if (kind == "box")            return 1;
    if (kind == "plane")          return 2;
    if (kind == "sphere")         return 3;
    if (kind == "cone")           return 4;
    if (kind == "cross_section" || kind == "section") return 5;
    return 6;
}

Level levelForRole(const std::string& role) {
    if (role.empty() || role == "unknown") return Level::Warn;
    return Level::Neutral;
}

const char* roleTip(const std::string& role) {
    if (role == "hole")   return "Interior cylinder — material is removed here. The diameter is "
                                 "fit-critical: confirm it with a pin gauge before you print a "
                                 "mating part.";
    if (role == "boss")   return "Exterior cylindrical pad standing proud of the body.";
    if (role == "shaft")  return "Exterior cylinder forming the part's own axis of rotation.";
    if (role == "pocket") return "Recessed flat — this plane is the floor of a pocket.";
    if (role == "pad")    return "Raised flat — this plane is the top of a pad.";
    return "The fitter could not classify this surface. Check it against the photos before you "
           "rely on it.";
}

}  // namespace

void kickFitPrimitives(AppState& st) {
    RevEngState& s = S();
    ModelState& m = s.model;
    const std::string sid = selectedSessionId();
    if (m.fitPending || sid.empty()) return;
    m.fitPending = true;
    m.fitErr.clear();
    nlohmann::json args = nlohmann::json::object();
    args["session_id"] = sid;
    st.rpc.call("reveng_fit_primitives", args, [sid](const core::RpcResult& r) {
        RevEngState& s2 = S();
        ModelState& m2 = s2.model;
        m2.fitPending = false;
        m2.fitsLoaded = true;
        if (selectedSessionId() != sid) return;

        ModelLocal& L = ML();
        L.sync(sid);
        if (!rpcOk(r, "reveng_fit_primitives", m2.fitErr)) return;
        m2.fitErr.clear();
        m2.fits.clear();
        L.details.clear();
        L.notes.clear();
        L.fitsMeshPath = r.result.value("mesh_path", std::string());

        auto add = [&](const nlohmann::json& f, const char* fallbackKind, double conf) {
            if (!f.is_object()) return;
            FitEntry e;
            e.kind       = f.value("kind", std::string(fallbackKind));
            e.confidence = f.contains("confidence") && f["confidence"].is_number()
                               ? f["confidence"].get<double>() : conf;
            e.source     = f.value("source", std::string());
            if (f.contains("params_mm")) e.paramsSummary = summarizeParams(f["params_mm"]);
            FitDetail d = parseFitDetail(f);
            d.kind = e.kind;
            m2.fits.push_back(std::move(e));
            L.details.push_back(std::move(d));
        };

        if (r.result.contains("fits") && r.result["fits"].is_array()) {
            for (const auto& f : r.result["fits"]) add(f, "unknown", 0.0);
        }
        // sections[] is a TOP-LEVEL sibling of fits[] with its own shape; fold each element into
        // the same index space so the list, the group headers and the detail vector stay parallel.
        // A slice has no inlier fraction, so it carries -1 and the confidence chip is suppressed.
        if (r.result.contains("sections") && r.result["sections"].is_array()) {
            for (const auto& f : r.result["sections"]) {
                if (!f.is_object()) continue;
                add(sectionToFit(f), "cross_section", -1.0);
            }
        }
        // notes[] — where the fitter degraded and why. Never swallowed.
        if (r.result.contains("notes") && r.result["notes"].is_array()) {
            for (const auto& nj : r.result["notes"]) {
                if (!nj.is_object()) continue;
                const std::string stage  = nj.value("stage", std::string());
                const std::string reason = nj.value("reason", std::string());
                const std::string hint   = nj.value("hint", std::string());
                if (reason.empty() && hint.empty()) continue;
                std::string line;
                if (!stage.empty()) line += stage + ": ";
                line += reason.empty() ? hint : reason;
                if (!reason.empty() && !hint.empty()) line += " \xe2\x80\x94 " + hint;
                L.notes.push_back(std::move(line));
            }
        }
    });
}

void kickBuildParametric(AppState& st, int strategyIdx) {
    RevEngState& s = S();
    ModelState& m = s.model;
    const std::string sid = selectedSessionId();
    if (m.buildPending || sid.empty()) return;
    m.buildPending = true;
    m.buildErr.clear();
    m.buildRunId.clear();

    nlohmann::json args = nlohmann::json::object();
    args["session_id"] = sid;
    if (strategyIdx == 1)      args["strategy"] = "mesh_fallback";
    else if (strategyIdx == 2) args["strategy"] = "override_approximate";

    st.rpc.call("reveng_build_parametric", args, [&st, sid](const core::RpcResult& r) {
        RevEngState& s2 = S();
        ModelState& m2 = s2.model;
        m2.buildPending = false;
        if (selectedSessionId() != sid) return;
        if (!rpcOk(r, "reveng_build_parametric", m2.buildErr)) {
            st.toast(ui::ToastLevel::Error, m2.buildErr);
            return;
        }
        m2.buildErr.clear();
        m2.buildRunId = r.result.value("run_id", std::string());
        m2.descLoaded = false;   // force a result refresh on the next poll
        st.toast(ui::ToastLevel::Success,
                 m2.buildRunId.empty() ? std::string("Parametric build launched")
                                       : ("Parametric build launched: " + m2.buildRunId));
    });
}

// describe_reveng_result — throttled refresh of the build outcome summary.
static void loadModelResult(AppState& st) {
    RevEngState& s = S();
    ModelState& m = s.model;
    const std::string sid = selectedSessionId();
    if (m.descPending || sid.empty()) return;
    if (m.descLoaded) return;
    m.descPending = true;
    nlohmann::json args = nlohmann::json::object();
    args["session_id"] = sid;
    st.rpc.call("describe_reveng_result", args, [sid](const core::RpcResult& r) {
        RevEngState& s2 = S();
        ModelState& m2 = s2.model;
        m2.descPending = false;
        m2.descLoaded  = true;
        if (selectedSessionId() != sid) return;

        ModelLocal& L = ML();
        L.sync(sid);
        L.matchScored = false;
        std::string hint;
        if (!rpcUnwrap(r, "describe_reveng_result", m2.descErr, L.descReason, hint)) return;
        m2.descErr.clear();
        L.descReason.clear();
        if (!r.result.contains("result") || !r.result["result"].is_object()) return;
        const auto& res = r.result["result"];
        m2.descDocId        = res.value("doc_id", std::string());
        m2.descStrategy     = res.value("strategy", std::string());
        m2.descMeshFallback = res.value("mesh_fallback", false);
        // A null match_pct means "built but never scored" — a different state from "not built".
        const auto mp = res.find("match_pct");
        if (mp != res.end() && mp->is_number()) {
            m2.descMatchPct = mp->get<double>();
            L.matchScored = true;
        } else {
            m2.descMatchPct = 0.0;
        }
    });
}

static ResultPhase modelResultPhase() {
    const ModelState& m = S().model;
    if (m.descPending && !m.descLoaded) return ResultPhase::Loading;
    if (!m.descErr.empty()) {
        const std::string& reason = ML().descReason;
        // The contract's "no result yet" is a state, not a failure.
        if (reason.find("no result") != std::string::npos ||
            m.descErr.find("no result") != std::string::npos) {
            return ResultPhase::NotBuilt;
        }
        return ResultPhase::Failed;
    }
    if (m.descLoaded && !m.descDocId.empty()) return ResultPhase::Built;
    if (m.descLoaded) return ResultPhase::NotBuilt;
    return ResultPhase::Loading;
}

static void drawFitCard(int index, const FitEntry& e, const FitDetail& d) {
    ImGui::PushID(index);

    // Title-case the kind for the card header without assuming a non-empty string.
    std::string label = d.kind.empty() ? std::string("Fit") : d.kind;
    label[0] = (char)std::toupper((unsigned char)label[0]);
    for (char& c : label) if (c == '_') c = ' ';
    char title[96];
    std::snprintf(title, sizeof(title), "%s #%d", label.c_str(), index + 1);

    if (kit::CardBegin("##re_fit", title, iconForKind(d.kind, d.role))) {
        if (e.confidence >= 0.0) {
            const std::string conf = kit::fmtPct(e.confidence, 0);
            kit::CardHeaderRight(chipWidth("conf", conf.c_str()));
            kit::ValueChip("conf", conf.c_str(), levelForConfidence(e.confidence),
                           "RANSAC inlier fraction — how much of the segmented surface actually "
                           "agrees with this primitive.");
        }

        bool anyChip = false;
        if (d.kind == "cylinder" || !d.role.empty()) {
            kit::StatusChip(d.role.empty() ? "unknown" : d.role.c_str(),
                            levelForRole(d.role), roleTip(d.role));
            anyChip = true;
        }
        if (d.hasThrough) {
            if (anyChip) ImGui::SameLine(0, M::reCtlGap());
            kit::StatusChip(d.through ? "through" : "blind", Level::Neutral,
                            d.through ? "Breaks out of the far side of the body."
                                      : "Bottoms out inside the body — depth is fit-critical.");
            anyChip = true;
        }
        if (!e.source.empty()) {
            if (anyChip) ImGui::SameLine(0, M::reCtlGap());
            kit::StatusChip(e.source.c_str(), Level::Neutral, "Fit source.");
            anyChip = true;
        }
        if (anyChip) kit::GapControl();

        if (d.hasDepth) kit::KeyValueRow("depth", kit::fmtMm(d.depthMm, 2).c_str(), Level::Ok);
        for (const auto& row : d.rows)
            kit::KeyValueRow(row.first.c_str(), row.second.c_str());
        if (d.hasResidual) {
            kit::KeyValueRow("residual", kit::fmtMm(d.residualMm, 3).c_str(),
                             d.residualMm < 0.2 ? Level::Ok
                                                : (d.residualMm < 1.0 ? Level::Warn
                                                                      : Level::Error));
        }
        if (d.hasInliers)
            kit::KeyValueRow("inlier points", fmtNum(d.inliers, "%.0f").c_str());
        if (!d.hasDepth && !d.hasResidual && d.rows.empty())
            kit::InlineStatus("No parameters reported for this fit.", Level::Warn);
    }
    kit::CardEnd();
    ImGui::PopID();
}

static void drawFitsCard(AppState& st) {
    RevEngState& s = S();
    ModelState& m = s.model;
    ModelLocal& L = ML();

    if (!kit::CardBegin("re_model_fits", "Primitive fits", "geometry_analysis")) {
        kit::CardEnd();
        return;
    }

    const char* stateLabel = m.fitPending ? "fitting"
                           : !m.fitErr.empty() ? "failed"
                           : !m.fits.empty() ? "fitted"
                           : m.fitsLoaded ? "no fits" : "not run";
    const Level stateLevel = m.fitPending ? Level::Busy
                           : !m.fitErr.empty() ? Level::Error
                           : !m.fits.empty() ? Level::Ok
                           : m.fitsLoaded ? Level::Warn : Level::Neutral;
    kit::CardHeaderRight(chipWidth(stateLabel));
    kit::StatusChip(stateLabel, stateLevel);

    dimWrapped("RANSAC segments the reconstructed mesh into primitives and hands them to the "
               "parametric build as hints. Holes and bosses found here become real features, so "
               "check the roles before you build.");

    // The artifact this stage works from.
    const std::string meshPath = !L.fitsMeshPath.empty() ? L.fitsMeshPath : s.artifactMeshPath;
    kit::GapControl();
    kit::Viewport3DPreview("##re_fit_mesh", ImVec2(0, M::reThumbLg()),
                           kit::PreviewSlot::Mesh,
                           meshPath.empty() ? "No mesh yet — run Reconstruct" : nullptr);
    if (!meshPath.empty()) {
        kit::GapControl();
        kit::KeyValueRow("Mesh", baseName(meshPath).c_str());
    }

    kit::GapControl();
    const std::string meshBlock = meshBackendBlock();
    const char* fitBlocked = m.fitPending ? "A fit is already running."
                           : !meshBlock.empty() ? meshBlock.c_str() : nullptr;
    if (kit::PrimaryButton(m.fitPending ? "Fitting\xe2\x80\xa6" : "Fit primitives",
                           fitBlocked == nullptr, fitBlocked)) {
        kickFitPrimitives(st);
    }
    if (!m.fits.empty()) {
        ImGui::SameLine(0, M::reCtlGap());
        kit::ValueChip("fits", fmtNum((double)m.fits.size(), "%.0f").c_str(), Level::Neutral);
    }
    kit::InlineStatus(m.fitErr.c_str(), Level::Error);

    // The fitter's own degradation report, before any list of results.
    if (!L.notes.empty()) {
        kit::GapSection();
        kit::SectionHeader("Fitter notes", "warning_badge");
        for (const std::string& note : L.notes)
            kit::InlineStatus(note.c_str(), Level::Warn);
    }

    if (m.fits.empty()) {
        kit::GapSection();
        if (m.fitsLoaded && m.fitErr.empty()) {
            if (kit::EmptyState("geometry_analysis", "No primitives found",
                                "The segmentation ran but nothing matched a cylinder, box, plane, "
                                "sphere or cone within tolerance. A cleaner mesh or a denser "
                                "reconstruction usually fixes this.",
                                "Go to Reconstruct")) {
                s.stage = Stage::Reconstruct;
            }
        } else if (!m.fitPending) {
            kit::EmptyState("geometry_analysis", "Nothing fitted yet",
                            "Run the fit to see which cylinders, holes, bosses and flats the "
                            "mesh actually contains.");
        }
        kit::CardEnd();
        return;
    }

    // ---- grouped rich list ----
    const size_t n = std::min(m.fits.size(), L.details.size());
    for (int rank = 0; rank <= 6; ++rank) {
        // Count first so the header can carry the group size.
        int count = 0;
        for (size_t i = 0; i < n; ++i)
            if (kindRank(L.details[i].kind) == rank) ++count;
        if (count == 0) continue;

        std::string groupKind;
        for (size_t i = 0; i < n; ++i)
            if (kindRank(L.details[i].kind) == rank) { groupKind = L.details[i].kind; break; }

        char header[96];
        std::snprintf(header, sizeof(header), "%s (%d)", pluralKind(groupKind), count);
        kit::GapSection();
        kit::SectionHeader(header, iconForKind(groupKind, std::string()));

        for (size_t i = 0; i < n; ++i) {
            if (kindRank(L.details[i].kind) != rank) continue;
            drawFitCard((int)i, m.fits[i], L.details[i]);
            kit::GapCards();
        }
    }
    kit::CardEnd();
}

static void drawBuildCard(AppState& st) {
    RevEngState& s = S();
    ModelState& m = s.model;
    const bool approximate = selectedSessionApproximate();

    if (!kit::CardBegin("re_model_build", "Parametric build", "extrude_boss")) {
        kit::CardEnd();
        return;
    }

    const char* stateLabel = m.buildPending ? "launching"
                           : !m.buildErr.empty() ? "failed"
                           : !m.buildRunId.empty() ? "running" : "idle";
    const Level stateLevel = m.buildPending ? Level::Busy
                           : !m.buildErr.empty() ? Level::Error
                           : !m.buildRunId.empty() ? Level::Ok : Level::Neutral;
    kit::CardHeaderRight(chipWidth(stateLabel));
    kit::StatusChip(stateLabel, stateLevel);

    kit::FieldLabel("Strategy",
                    "Auto rebuilds editable features from the primitive fits plus AI feature "
                    "recognition. Mesh-body fallback wraps the mesh as a single non-editable body "
                    "— honest, but you cannot change a dimension afterwards.");
    int seg = (m.strategy == 1) ? 1 : 0;
    if (kit::SegmentedControl("##re_build_strategy", kStrategyLabels, 2, &seg,
                              ImGui::GetContentRegionAvail().x)) {
        m.strategy = seg;
    }

    kit::GapControl();
    const std::string meshBlock = meshBackendBlock();
    const char* buildBlocked = nullptr;
    if (m.buildPending)          buildBlocked = "A build is already launching.";
    else if (!meshBlock.empty()) buildBlocked = meshBlock.c_str();
    else if (approximate)        buildBlocked =
        "This session is tagged approximate (single-photo tier). Parametric output from one photo "
        "is reference/preview only, so the honesty gate blocks the normal build. Use "
        "\"Override (approximate)\" below if you accept that.";

    if (kit::PrimaryButton(m.buildPending ? "Launching\xe2\x80\xa6" : "Build parametric model",
                           buildBlocked == nullptr, buildBlocked)) {
        kickBuildParametric(st, m.strategy);
    }
    if (!m.buildRunId.empty()) {
        ImGui::SameLine(0, M::reCtlGap());
        kit::ValueChip("run", m.buildRunId.c_str(), Level::Neutral);
    }

    if (approximate) {
        kit::GapSection();
        kit::SectionHeader("Escape hatch", "warning_badge");
        kit::HonestyTag("approximate",
                        "Single-photo reconstruction. Geometry is plausible, not measured.");
        kit::GapControl();
        kit::InlineStatus(
            "Overriding drops the honesty gate, not the honesty. The result stays tagged "
            "approximate everywhere it is used, and every dimension it produces is "
            "reference-only.", Level::Warn);
        kit::GapControl();
        if (kit::DangerButton("Override (approximate)", !m.buildPending,
                              "A build is already launching.")) {
            kickBuildParametric(st, 2);
        }
    }

    kit::InlineStatus(m.buildErr.c_str(), Level::Error);
    kit::CardEnd();
}

static void drawModelResultCard(AppState& st) {
    RevEngState& s = S();
    ModelState& m = s.model;
    ModelLocal& L = ML();

    loadModelResult(st);

    if (!kit::CardBegin("re_model_result", "Build result", "results")) {
        kit::CardEnd();
        return;
    }

    const float refreshW = M::reBtnW();
    kit::CardHeaderRight(refreshW);
    if (kit::SecondaryButton("Refresh", !m.descPending, "Already checking.",
                             ImVec2(refreshW, ImGui::GetFrameHeight()))) {
        m.descLoaded = false;
    }

    switch (modelResultPhase()) {
        case ResultPhase::Loading:
            kit::ProgressBar(-1.0f, "Reading the session result\xe2\x80\xa6", Level::Busy);
            break;

        case ResultPhase::NotBuilt:
            kit::EmptyState("results", "Nothing built yet",
                            "No document has been produced for this session. Run a parametric "
                            "build above — the result, its match score and its honesty tier "
                            "appear here.");
            break;

        case ResultPhase::Failed:
            if (kit::BlockedState("Could not read the build result",
                                  m.descErr.c_str(),
                                  "Check the worker log in the Console panel, then refresh.",
                                  "Refresh")) {
                m.descLoaded = false;
            }
            break;

        case ResultPhase::Built: {
            kit::Viewport3DPreview("##re_result_preview", ImVec2(0, M::reThumbLg()),
                                   kit::PreviewSlot::Parametric, nullptr);
            kit::GapControl();
            kit::KeyValueRow("Document", m.descDocId.c_str(), Level::Ok);
            kit::KeyValueRow("Strategy",
                             m.descStrategy.empty() ? "(not reported)" : m.descStrategy.c_str(),
                             m.descStrategy.empty() ? Level::Warn : Level::Neutral);

            kit::GapSection();
            kit::SectionHeader("Geometry match", "check");
            if (L.matchScored) {
                const Level ml = levelForMatch(m.descMatchPct);
                const std::string pct = kit::fmtPct(m.descMatchPct / 100.0, 1);
                kit::ProgressBar((float)(m.descMatchPct / 100.0), pct.c_str(), ml);
                kit::GapControl();
                kit::ValueChip("match", pct.c_str(), ml,
                               "Common volume between the rebuilt solid and the scan mesh, over "
                               "the larger of the two.");
                ImGui::SameLine(0, M::reCtlGap());
                kit::StatusChip(matchVerdict(m.descMatchPct), ml,
                                "Verdict bands: 98 % and above passes, 95-98 % is conditional "
                                "(inspect the deviation map), below 95 % fails.");
            } else {
                kit::StatusChip("built, not scored", Level::Warn,
                                "The build produced a document but no geometry comparison has run "
                                "yet, so there is no match_pct. Run the Review stage's deviation "
                                "check to score it.");
                kit::GapControl();
                dimWrapped("A missing score is not a passing score. Until the rebuilt solid is "
                           "compared against the scan you have no evidence the shape is right.");
            }

            if (m.descMeshFallback) {
                kit::GapSection();
                kit::SectionHeader("Honesty", "warning_badge");
                kit::HonestyTag("low",
                                "mesh_fallback: the delivered geometry is a wrapped mesh body, not "
                                "editable parametric features. Dimensions cannot be edited and "
                                "carry the mesh's own faceting error.");
            }
            break;
        }
    }
    kit::CardEnd();
}

void drawModelPanel(AppState& st) {
    const std::string sid = selectedSessionId();
    ML().sync(sid);

    kit::SectionHeader("Model \xe2\x80\x94 turn the scan into editable geometry", "extrude_boss");
    if (guardNoSession("Fitting and building operate on a session. Pick one in the rail, or start "
                       "a capture in the Sources stage.")) {
        return;
    }

    drawFitsCard(st);
    kit::GapCards();
    drawBuildCard(st);
    kit::GapCards();
    drawModelResultCard(st);
}

// =================================================================================================
// Deliver stage
// =================================================================================================

static void kickAttachResult(AppState& st) {
    RevEngState& s = S();
    DeliverState& d = s.deliver;
    const std::string sid = selectedSessionId();
    if (d.attachPending || sid.empty() || d.partRefBuf[0] == '\0') return;
    d.attachPending = true;
    d.attachErr.clear();
    d.attachOk.clear();
    DL().attachPath.clear();
    nlohmann::json args = nlohmann::json::object();
    args["session_id"] = sid;
    args["part_ref"]   = std::string(d.partRefBuf);
    st.rpc.call("reveng_attach_result", args, [&st, sid](const core::RpcResult& r) {
        RevEngState& s2 = S();
        DeliverState& d2 = s2.deliver;
        d2.attachPending = false;
        if (selectedSessionId() != sid) return;
        if (!rpcOk(r, "reveng_attach_result", d2.attachErr)) {
            st.toast(ui::ToastLevel::Error, d2.attachErr);
            return;
        }
        d2.attachErr.clear();
        const std::string geo = r.result.value("geometry_ref", std::string());
        const std::string ref = r.result.value("part_ref", std::string());
        DL().attachPath = geo.empty() ? ref : geo;
        d2.attachOk = "Attached to the parts hub.";
        st.toast(ui::ToastLevel::Success, "Attached to parts hub" +
                                          (DL().attachPath.empty()
                                               ? std::string()
                                               : (" \xe2\x80\x94 " + DL().attachPath)));
    });
}

static void kickExportBundle(AppState& st) {
    RevEngState& s = S();
    DeliverState& d = s.deliver;
    const std::string sid = selectedSessionId();
    if (d.exportPending || sid.empty() || d.exportDestBuf[0] == '\0') return;
    d.exportPending = true;
    d.exportErr.clear();
    d.exportOk.clear();
    DL().exportPath.clear();
    nlohmann::json args = nlohmann::json::object();
    args["session_id"] = sid;
    args["dest"]       = std::string(d.exportDestBuf);
    st.rpc.call("export_reveng_bundle", args, [&st, sid](const core::RpcResult& r) {
        RevEngState& s2 = S();
        DeliverState& d2 = s2.deliver;
        d2.exportPending = false;
        if (selectedSessionId() != sid) return;
        if (!rpcOk(r, "export_reveng_bundle", d2.exportErr)) {
            st.toast(ui::ToastLevel::Error, d2.exportErr);
            return;
        }
        d2.exportErr.clear();
        DL().exportPath = r.result.value("path", std::string());
        d2.exportOk = "Bundle exported.";
        st.toast(ui::ToastLevel::Success,
                 DL().exportPath.empty() ? std::string("Bundle exported")
                                         : ("Bundle exported: " + DL().exportPath));
    });
}

// The honesty tier the delivered geometry actually earns.
static const char* deliveredTier() {
    const RevEngState& s = S();
    const ScaleState& sc = s.scale;
    const ModelState& m = s.model;
    if (selectedSessionApproximate() || m.descMeshFallback) return "approximate";
    if (!sc.mirrored || !(sc.mmPerUnit > 0.0))              return "approximate";
    if (!sc.confirmed)                                      return "low";
    if (!ML().matchScored)                                  return "medium";
    if (m.descMatchPct >= 98.0)                             return "high";
    if (m.descMatchPct >= 95.0)                             return "medium";
    return "low";
}

static const char* sourceLaneLabel(SourceMode mode) {
    switch (mode) {
        case SourceMode::Scan:        return "Scan (photos / video)";
        case SourceMode::Datasheet:   return "Datasheet";
        case SourceMode::Web:         return "Web / part number";
        case SourceMode::FromLibrary: return "Parts library seed";
    }
    return "unknown";
}

static void drawDeliverHeroCard() {
    RevEngState& s = S();
    ModelState& m = s.model;
    ScaleState& sc = s.scale;

    const std::string name = selectedSessionName();
    if (!kit::CardBegin("re_deliver_hero", name.empty() ? "Delivered part" : name.c_str(),
                        "part")) {
        kit::CardEnd();
        return;
    }

    const char* tier = deliveredTier();
    kit::CardHeaderRight(chipWidth(tier));
    kit::HonestyTag(tier, "Tier of the geometry this stage is about to hand off.");

    const bool built = !m.descDocId.empty();
    const bool haveMesh = !s.artifactMeshPath.empty();
    const char* emptyLabel = built ? nullptr
                           : haveMesh ? nullptr
                           : "Nothing built yet \xe2\x80\x94 run the Model stage";
    kit::Viewport3DPreview("##re_deliver_hero_vp",
                           ImVec2(0, M::reThumbLg()),
                           built ? kit::PreviewSlot::Parametric : kit::PreviewSlot::Mesh,
                           emptyLabel);

    kit::GapControl();
    if (built) {
        kit::ValueChip("doc", m.descDocId.c_str(), Level::Ok);
    } else {
        kit::StatusChip(haveMesh ? "mesh only" : "not built",
                        haveMesh ? Level::Warn : Level::Error,
                        haveMesh
                            ? "A reconstructed mesh exists but no parametric document has been "
                              "built. Anything you deliver now is a mesh, not editable geometry."
                            : "Run the Model stage before delivering.");
    }
    ImGui::SameLine(0, M::reCtlGap());
    if (ML().matchScored) {
        kit::ValueChip("match", kit::fmtPct(m.descMatchPct / 100.0, 1).c_str(),
                       levelForMatch(m.descMatchPct));
    } else if (built) {
        kit::StatusChip("unscored", Level::Warn,
                        "Built, but never compared against the scan. No evidence the shape is "
                        "right.");
    }
    ImGui::SameLine(0, M::reCtlGap());
    kit::StatusChip(sc.confirmed ? "scale confirmed" : "scale unconfirmed",
                    sc.confirmed ? Level::Ok : Level::Error,
                    sc.confirmed
                        ? "Dimensions are backed by a confirmed physical scale."
                        : "Without a confirmed scale every dimension below is a ratio, not a "
                          "measurement.");

    if (!sc.confirmed || !built) {
        kit::GapControl();
        kit::InlineStatus(!built
                              ? "Deliverable is incomplete: there is no parametric document yet."
                              : "Deliverable is not dimensionally defensible: the scale is not "
                                "confirmed.",
                          Level::Warn);
    }
    kit::CardEnd();
}

static void drawAttachCard(AppState& st) {
    RevEngState& s = S();
    DeliverState& d = s.deliver;
    DeliverLocal& L = DL();

    if (!kit::CardBegin("re_deliver_attach", "Attach to Parts Hub", "bom")) {
        kit::CardEnd();
        return;
    }

    const char* stateLabel = d.attachPending ? "attaching"
                           : !d.attachErr.empty() ? "failed"
                           : !d.attachOk.empty() ? "attached" : "idle";
    const Level stateLevel = d.attachPending ? Level::Busy
                           : !d.attachErr.empty() ? Level::Error
                           : !d.attachOk.empty() ? Level::Ok : Level::Neutral;
    kit::CardHeaderRight(chipWidth(stateLabel));
    kit::StatusChip(stateLabel, stateLevel);

    dimWrapped("Publishes the built geometry against a parts-hub entry so assemblies, BOMs and "
               "sourcing can reference it like any catalogue part.");

    kit::FieldLabel("part_ref", "The parts-hub id to attach to, e.g. "
                                "catalog:grabcad:bracket-123.");
    ImGui::SetNextItemWidth(ImGui::GetContentRegionAvail().x);
    ImGui::InputTextWithHint("##re_part_ref", "catalog:grabcad:bracket-123",
                             d.partRefBuf, sizeof(d.partRefBuf));

    kit::GapControl();
    const bool nothingToAttach = s.model.descDocId.empty() && s.artifactMeshPath.empty();
    const char* blocked = nullptr;
    if (d.attachPending)          blocked = "An attach is already running.";
    else if (nothingToAttach)     blocked = "Nothing to attach — this session has neither a built "
                                            "document nor a reconstructed mesh. Run the Model "
                                            "stage first.";
    else if (d.partRefBuf[0] == '\0') blocked = "Type the parts-hub part_ref to attach to.";

    if (kit::PrimaryButton(d.attachPending ? "Attaching\xe2\x80\xa6" : "Attach",
                           blocked == nullptr, blocked)) {
        kickAttachResult(st);
    }
    if (!L.attachPath.empty()) {
        ImGui::SameLine(0, M::reCtlGap());
        if (kit::SecondaryButton("Copy ref")) ImGui::SetClipboardText(L.attachPath.c_str());
    }

    if (!L.attachPath.empty()) {
        kit::GapControl();
        kit::KeyValueRow("geometry_ref", L.attachPath.c_str(), Level::Ok);
    }
    kit::InlineStatus(d.attachOk.c_str(), Level::Ok);
    kit::InlineStatus(d.attachErr.c_str(), Level::Error);
    kit::CardEnd();
}

static void drawExportCard(AppState& st) {
    DeliverState& d = S().deliver;
    DeliverLocal& L = DL();

    if (!kit::CardBegin("re_deliver_export", "Export bundle", "save")) {
        kit::CardEnd();
        return;
    }

    const char* stateLabel = d.exportPending ? "exporting"
                           : !d.exportErr.empty() ? "failed"
                           : !d.exportOk.empty() ? "written" : "idle";
    const Level stateLevel = d.exportPending ? Level::Busy
                           : !d.exportErr.empty() ? Level::Error
                           : !d.exportOk.empty() ? Level::Ok : Level::Neutral;
    kit::CardHeaderRight(chipWidth(stateLabel));
    kit::StatusChip(stateLabel, stateLevel);

    dimWrapped("Zips the whole session — photos, sparse model, mesh, fits, the scale solve and "
               "the provenance chain — so the numbers can be audited or handed to someone else.");

    kit::FieldLabel("Destination", "Where the .zip is written.");
    const float browseW = M::reBtnW();
    ImGui::SetNextItemWidth(ImGui::GetContentRegionAvail().x - browseW - M::reCtlGap());
    ImGui::InputTextWithHint("##re_export_dest", "reveng_bundle.zip",
                             d.exportDestBuf, sizeof(d.exportDestBuf));
    ImGui::SameLine(0, M::reCtlGap());
    if (kit::SecondaryButton("Browse\xe2\x80\xa6", true, nullptr,
                             ImVec2(browseW, ImGui::GetFrameHeight()))) {
        const std::string path = ui::saveFileDialog(
            "RE bundle (*.zip)\0*.zip\0All files (*.*)\0*.*\0", "zip", "reveng_bundle.zip");
        if (!path.empty())
            std::snprintf(d.exportDestBuf, sizeof(d.exportDestBuf), "%s", path.c_str());
    }

    kit::GapControl();
    const char* blocked = nullptr;
    if (d.exportPending)               blocked = "An export is already running.";
    else if (d.exportDestBuf[0] == '\0') blocked = "Pick a destination .zip first.";

    if (kit::PrimaryButton(d.exportPending ? "Exporting\xe2\x80\xa6" : "Export bundle",
                           blocked == nullptr, blocked)) {
        kickExportBundle(st);
    }
    if (!L.exportPath.empty()) {
        ImGui::SameLine(0, M::reCtlGap());
        if (kit::SecondaryButton("Copy path")) ImGui::SetClipboardText(L.exportPath.c_str());
    }

    if (!L.exportPath.empty()) {
        kit::GapControl();
        kit::KeyValueRow("Wrote", L.exportPath.c_str(), Level::Ok);
    }
    kit::InlineStatus(d.exportOk.c_str(), Level::Ok);
    kit::InlineStatus(d.exportErr.c_str(), Level::Error);
    kit::CardEnd();
}

// Everything an engineer needs to defend these numbers six months from now.
static void drawProvenanceCard() {
    RevEngState& s = S();
    const ScaleState& sc = s.scale;
    const ModelState& m = s.model;

    if (!kit::CardBegin("re_deliver_prov", "Provenance", "history")) {
        kit::CardEnd();
        return;
    }

    const char* tier = deliveredTier();
    kit::CardHeaderRight(chipWidth(tier));
    kit::HonestyTag(tier);

    dimWrapped("How this part was derived, end to end. Copy it into the design record — a "
               "dimension without a provenance chain is a guess with extra steps.");
    kit::GapControl();

    const int imageCount = (s.selected >= 0 && s.selected < (int)s.sessions.size())
                               ? s.sessions[(size_t)s.selected].images
                               : (int)s.galleryImages.size();

    const std::string scaleVal =
        (!sc.mirrored || !(sc.mmPerUnit > 0.0))
            ? std::string("not set")
            : (sc.curMethod.empty() ? std::string("(unknown)") : sc.curMethod) +
                  "  \xc2\xb7  " + fmtNum(sc.mmPerUnit) + " mm/unit  \xc2\xb7  residual " +
                  kit::fmtPct(sc.residualPct / 100.0, 2);

    std::string reconVal;
    if (!s.artifactSplatPath.empty()) reconVal = "splat + ";
    if (!s.artifactMeshPath.empty())  reconVal += "mesh + ";
    if (!s.artifactSparseDir.empty()) reconVal += "sparse + ";
    if (reconVal.empty()) reconVal = "none";
    else                  reconVal.erase(reconVal.size() - 3);

    const std::string matchVal = ML().matchScored
        ? (kit::fmtPct(m.descMatchPct / 100.0, 1) + "  (" + matchVerdict(m.descMatchPct) + ")")
        : (m.descDocId.empty() ? std::string("not built") : std::string("built, not scored"));

    kit::KeyValueRow("Session", selectedSessionId().c_str());
    kit::KeyValueRow("Source lane", sourceLaneLabel(s.sourceMode));
    kit::KeyValueRow("Images", fmtNum((double)imageCount, "%.0f").c_str());
    kit::KeyValueRow("Scale", scaleVal.c_str(),
                     (sc.mirrored && sc.mmPerUnit > 0.0) ? levelForResidual(sc.residualPct)
                                                         : Level::Error);
    kit::KeyValueRow("Scale confirmed", sc.confirmed ? "yes" : "no",
                     sc.confirmed ? Level::Ok : Level::Error);
    kit::KeyValueRow("Reconstruction", reconVal.c_str(),
                     reconVal == "none" ? Level::Error : Level::Ok);
    kit::KeyValueRow("Primitive fits", fmtNum((double)m.fits.size(), "%.0f").c_str());
    kit::KeyValueRow("Build strategy",
                     m.descStrategy.empty()
                         ? (m.descDocId.empty() ? "not built" : "(not reported)")
                         : m.descStrategy.c_str(),
                     m.descStrategy.empty() ? Level::Warn : Level::Neutral);
    kit::KeyValueRow("Geometry match", matchVal.c_str(),
                     ML().matchScored ? levelForMatch(m.descMatchPct) : Level::Warn);
    if (m.descMeshFallback)
        kit::KeyValueRow("Mesh fallback", "yes — not editable parametric", Level::Warn);

    kit::GapControl();
    if (kit::SecondaryButton("Copy provenance")) {
        std::string txt;
        txt += "Reverse-engineering provenance\n";
        txt += "  session:        " + selectedSessionId() + "\n";
        txt += "  name:           " + selectedSessionName() + "\n";
        txt += "  source lane:    " + std::string(sourceLaneLabel(s.sourceMode)) + "\n";
        txt += "  images:         " + fmtNum((double)imageCount, "%.0f") + "\n";
        txt += "  scale:          " + scaleVal + "\n";
        txt += "  scale confirmed:" + std::string(sc.confirmed ? " yes" : " no") + "\n";
        txt += "  reconstruction: " + reconVal + "\n";
        txt += "  primitive fits: " + fmtNum((double)m.fits.size(), "%.0f") + "\n";
        txt += "  build strategy: " +
               (m.descStrategy.empty() ? std::string("(not reported)") : m.descStrategy) + "\n";
        txt += "  geometry match: " + matchVal + "\n";
        txt += "  honesty tier:   " + std::string(tier) + "\n";
        ImGui::SetClipboardText(txt.c_str());
    }
    kit::CardEnd();
}

void drawDeliverPanel(AppState& st) {
    const std::string sid = selectedSessionId();
    DL().sync(sid);
    ML().sync(sid);

    kit::SectionHeader("Deliver \xe2\x80\x94 hand the part off with its evidence", "part");
    if (guardNoSession("Delivery operates on a session. Pick one in the rail, or start a capture "
                       "in the Sources stage.")) {
        return;
    }

    // The trust hero reads describe_reveng_result, so fetch it here too — a user can walk
    // straight from Reconstruct to Deliver without ever opening the Model stage.
    loadModelResult(st);

    drawDeliverHeroCard();
    kit::GapCards();
    drawAttachCard(st);
    kit::GapCards();
    drawExportCard(st);
    kit::GapCards();
    drawScaleMatCard(st, DL().matTex, "re_deliver_mat");
    kit::GapCards();
    drawProvenanceCard();
}

}  // namespace reveng
}  // namespace ui
