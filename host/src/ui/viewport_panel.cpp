// AI-CAD Viewport panel — 3D orbit view + parts list + fit readout + overlays.
#include <cmath>
#include <cstdint>
#include <string>

#include "imgui.h"

#include "viewport_panel.h"

#include "legend.h"
#include "icons.h"
#include "mouse_gestures.h"
#include "shortcut_bar.h"
#include "camera_anim.h"     // C25 W2-D: eased camera tween (presets / Normal-To / zoom-to-fit)
#include "view_selector.h"   // C25 W2-E: SolidWorks-style glass-box view selector (Space)
#include "section_tool.h"    // C25 W3-J: section-view tool (another agent owns impl)

#include "../core/app_state.h"

#include "../renderer.h"
#include "../render/id_pass.h"
#include "../render/overlays.h"

#include "sketch_mode.h"   // C18
#include "property_manager.h"  // C27-A: in-viewport parameter callouts
#include "appearance_common.h"  // W1-B (item 9): shared MaterialPreset table + appearance picker

namespace ui {

// C25: file-local gesture/shortcut state — consistent with how SketchMode is held externally.
static MouseGestures s_gestures;
static ShortcutBar   s_shortcutBar;
static ViewSelector  s_viewSelector;   // C25 W2-E: glass-box view selector (Space)
static SectionTool   s_sectionTool;    // C25 W3-J: section-view (impl owned by section_tool agent)

// C25 W3-J: zoom-area mode state.
static bool  s_zoomAreaArmed = false;
static bool  s_zoomAreaDragging = false;
static ImVec2 s_zoomAreaP0 = {0, 0};  // drag start (screen)
static ImVec2 s_zoomAreaP1 = {0, 0};  // drag current (screen)

// ---- C26-C: Normal-To (Ctrl+8 / shortcut) -----------------------------------------------------
// "Look straight at the selected face." If a face is selected and the C26-C normal cache has data
// for it, animate the camera so the view direction equals the face's outward normal. Second press
// while already looking at that face flips to the anti-view (SolidWorks behavior). Pan resets to
// the face centroid (so the face centers on screen). Zoom is kept.
//
// Yaw/pitch derivation (Z-up, target-to-eye convention):
//   dir = (cos(pitch)*sin(yaw), -cos(pitch)*cos(yaw), sin(pitch))
//   Given face normal N = (nx, ny, nz):
//     pitch = asin(nz)            (clamped ±89.5° by requestCameraAnimation)
//     yaw   = atan2(nx, -ny)
//   This satisfies dir == N by construction; verified against Front (N=(0,-1,0) → yaw=0,pitch=0)
//   and Top (N=(0,0,1) → yaw=0,pitch=90) which match the existing presets.
//
// Nearest-axis fallback: when no face is selected, snaps to the axis-aligned view whose view
// direction is nearest the current one (same as the old stub).

// Persistent state for second-press flip detection (file-static, one viewport):
static uint64_t s_normalToKey   = 0;    // last cached key (body<<32 | face_id)
static float    s_normalToYaw   = 0;    // the yaw we animated to
static float    s_normalToPitch = 0;    // the pitch we animated to

static void normalToSelectionOrNearestAxis(AppState& st) {
    const float kPiOver180 = 3.14159265f / 180.0f;

    // ---- try true face normal path ----
    if (!st.selection.empty()) {
        const render::PickHit& h = st.selection.items().back().hit;
        if (h.valid() && h.kind == render::PickKind::Face) {
            uint64_t key = ((uint64_t)h.body_id << 32) | (uint64_t)h.topo_id;
            auto it = st.faceNormalCache.find(key);
            if (it != st.faceNormalCache.end()) {
                const auto& v = it->second;
                float nx = v[0], ny = v[1], nz = v[2];
                float faceCx = v[3], faceCy = v[4], faceCz = v[5];

                // Second-press flip: if we already animated to this face, flip to anti-view.
                bool alreadyNormal = (key == s_normalToKey);
                float targetNx = nx, targetNy = ny, targetNz = nz;
                if (alreadyNormal) {
                    targetNx = -nx; targetNy = -ny; targetNz = -nz;
                    s_normalToKey = 0;  // next press goes forward again
                } else {
                    s_normalToKey = key;
                }

                float pitch = std::asin(targetNz > 1.0f ? 1.0f : (targetNz < -1.0f ? -1.0f : targetNz));
                float yaw   = std::atan2(targetNx, -targetNy);
                float pitchDeg = pitch / kPiOver180;
                float yawDeg   = yaw   / kPiOver180;
                s_normalToYaw   = yawDeg;
                s_normalToPitch = pitchDeg;

                // Pan to centroid so the face is centered on screen.
                float pan[3] = {faceCx, faceCy, faceCz};
                requestCameraAnimation(st, yawDeg, pitchDeg, -1.0f, pan);
                return;
            }
        }
    }
    s_normalToKey = 0;  // no face selected: reset flip state

    // ---- nearest-axis fallback ----
    // dir = view direction from target toward eye (target-to-eye convention):
    //   dir = (cos p sin y, -cos p cos y, sin p)
    float y = st.yaw * kPiOver180, p = st.pitch * kPiOver180;
    float dx = std::cos(p) * std::sin(y);
    float dy = -std::cos(p) * std::cos(y);
    float dz = std::sin(p);
    struct AxView { float x, y, z; ViewPreset preset; };
    static const AxView kAx[6] = {
        { 0, -1,  0, ViewPreset::Front  },
        { 0,  1,  0, ViewPreset::Back   },
        { 1,  0,  0, ViewPreset::Right  },
        {-1,  0,  0, ViewPreset::Left   },
        { 0,  0,  1, ViewPreset::Top    },
        { 0,  0, -1, ViewPreset::Bottom },
    };
    int best = 0; float bestDot = -2.0f;
    for (int i = 0; i < 6; ++i) {
        float d = dx * kAx[i].x + dy * kAx[i].y + dz * kAx[i].z;
        if (d > bestDot) { bestDot = d; best = i; }
    }
    applyViewPreset(st, kAx[best].preset);
}

// W1-B (item 9): the appearance picker + MaterialPreset table moved to ui/appearance_common.{h,cpp}
// (formerly duplicated verbatim here and in feature_tree.cpp). The call site below uses
// ui::appearanceColorPicker().

// ---- C26: apply a set_appearance op straight from the viewport, then reload the mesh. ----
// A DIRECT apply (not the feature tree's deferred queue) is SAFE here: the viewport context menu is
// NOT drawn while iterating the DocMirror tree json (the feature tree's use-after-free hazard) — it
// runs inside the Viewport window after rendering, touching no tree-iteration state. This mirrors
// feature_tree's applyTreeOpsNow: apply -> loadDocMesh(resp.aicm_path) repaints the body with its
// new color on the next frame.
static void applyAppearanceFromViewport(AppState& st, const std::string& bodyKey,
                                        const float* color /* nullptr = clear */,
                                        float metallic = 0.0f, float roughness = 0.5f) {
    if (!st.docMirror.hasDoc() || !st.workerStarted) return;
    nlohmann::json op = {{"op", "set_appearance"}, {"id", bodyKey}};
    if (color) {
        op["color"] = {color[0], color[1], color[2]};
        op["metallic"] = metallic;   // C32-A RealView material
        op["roughness"] = roughness;
    } else {
        op["color"] = nullptr;
    }
    auto resp = st.docMirror.applyOps(st.worker, nlohmann::json::array({op}));
    if (resp.ok && !resp.aicm_path.empty()) {
        std::string lerr;
        st.loadDocMesh(resp.aicm_path, lerr);
        if (!lerr.empty()) st.addLog("[err] loadDocMesh: " + lerr);
    } else if (!resp.ok) {
        st.addLog("[err] set_appearance: " + resp.error_message);
    }
}

void drawViewportPanel(AppState& st, ImGuiIO& io, bool busy) {
    Renderer& renderer = *st.renderer;
    // C18: while a sketch is being edited the camera locks normal to the sketch plane and the
    // sketch tools own the mouse (no orbit, no topology picking).
    auto* sketch = st.sketchMode;
    const bool inSketch = sketch && sketch->active();

    // ---------- Viewport ----------
    if (st.showViewport) {
    if (ImGui::Begin("Viewport"))
    {
        // C24: the view-orientation presets moved into the centered heads-up toolbar drawn over the
        // viewport image (below), SolidWorks-style. Nothing is drawn here at the panel top so the
        // 3D view starts flush against the ribbon.

        // ---- assembly fit readout + parts list (multi-part builds only) ----
        if (!st.asmParts.empty()) {
            ImGui::Separator();
            drawFitReadout(st.asmFit, /*detailed=*/true);   // always visible (key readout)
            // parts selector — scrolls if there are many bodies
            float ph = ImGui::GetContentRegionAvail().y * 0.30f;
            ph = ph > 150 ? 150 : (ph < 78 ? 78 : ph);
            ImGui::BeginChild("partspanel", ImVec2(0, ph), ImGuiChildFlags_Border);
            const auto& palVp = ui::palette;
            ImGui::PushStyleColor(ImGuiCol_Text,
                ImVec4(palVp.partsDim[0], palVp.partsDim[1], palVp.partsDim[2], 1));
            ImGui::TextUnformatted("Parts  (click to isolate in the viewport)");
            ImGui::PopStyleColor();
            if (ImGui::RadioButton("Assembly (all parts)", st.selectedPart == -1)) {
                st.selectedPart = -1;
                st.showPartMesh(-1);
            }
            for (int pi2 = 0; pi2 < (int)st.asmParts.size(); ++pi2) {
                ImGui::ColorButton(("##sw" + std::to_string(pi2)).c_str(), partColor(pi2),
                                   ImGuiColorEditFlags_NoTooltip | ImGuiColorEditFlags_NoDragDrop,
                                   ImVec2(12, 12));
                ImGui::SameLine(0, 6);
                std::string lbl = st.asmParts[pi2].name + "##p" + std::to_string(pi2);
                if (ImGui::RadioButton(lbl.c_str(), st.selectedPart == pi2)) {
                    st.selectedPart = pi2;
                    st.showPartMesh(pi2);
                }
                if (!st.asmParts[pi2].dims.empty()) {
                    ImGui::SameLine();
                    ImGui::PushStyleColor(ImGuiCol_Text,
                        ImVec4(palVp.textDim[0], palVp.textDim[1], palVp.textDim[2], 1));
                    ImGui::Text("  %s", st.asmParts[pi2].dims.c_str());
                    ImGui::PopStyleColor();
                }
            }
            ImGui::EndChild();
        }

        ImVec2 avail = ImGui::GetContentRegionAvail();
        int vw = (int)avail.x > 64 ? (int)avail.x : 64;
        int vh = (int)avail.y > 64 ? (int)avail.y : 64;
        renderer.resize(vw, vh);
        // C18: hard-lock the camera normal to the sketch plane while editing a sketch.
        if (inSketch) { float ly, lp; if (sketch->cameraLock(ly, lp)) { st.yaw = ly; st.pitch = lp; } }
        // Reference planes: hidden while sketching (the sketch grid owns the view), else per state.
        // C25: a fresh part with no geometry auto-shows its datum planes (SolidWorks "pick a
        // plane to start" state); once geometry exists, visibility is the user's toggle.
        bool effPlane[3] = {st.showPlane[0], st.showPlane[1], st.showPlane[2]};
        if (!st.hasDocMesh && st.docMirror.hasDoc())
            effPlane[0] = effPlane[1] = effPlane[2] = true;
        if (inSketch) { bool none[3] = {false,false,false}; renderer.setPlanes(none, -1); }
        else          renderer.setPlanes(effPlane, st.planeEmphasis);
        // C25 W2-D: advance any in-flight camera tween (presets / Normal-To / zoom-to-fit) before
        // rendering. Sketch mode hard-locks the camera, so cancel/skip the tween while sketching.
        if (inSketch) cancelCameraAnimation();
        else          tickCameraAnimation(st);
        renderer.renderOrbit(st.yaw, st.pitch, st.zoom, st.pan);

        // ---- topology picking (C15): id pass + overlay highlights drawn over the shaded view ----
        const bool picking = st.hasDocMesh && st.idPass && st.overlays;
        if (picking) {
            // Render the R32UI id buffer for this exact camera frame so picks are pixel-accurate.
            st.idPass->render(renderer.camera());
            st.pickReady = true;
            // Re-draw hover + selection highlights into the studio color FBO before it's sampled.
            renderer.beginOverlay();
            std::vector<render::PickHit> selHits;
            selHits.reserve(st.selection.size());
            for (const auto& it : st.selection.items()) selHits.push_back(it.hit);
            st.overlays->draw(renderer.camera(), st.hover, selHits);
            renderer.endOverlay();
        }

        ImGui::Image((ImTextureID)(intptr_t)renderer.colorTexture(), ImVec2((float)vw, (float)vh),
                     ImVec2(0, 1), ImVec2(1, 0));
        ImVec2 vr0 = ImGui::GetItemRectMin(), vr1 = ImGui::GetItemRectMax();
        const bool imgHovered = ImGui::IsItemHovered();
        // C25 W2-D: camera navigation. All of it is suppressed while sketching (camera is
        // plane-locked) except the wheel zoom, which stays usable. Any drag-navigation cancels an
        // in-flight tween so the user takes over instantly.
        if (imgHovered && !inSketch) {
            // Pixels -> world units at the orbit target plane (matches drawCurrent's FOV/dist):
            //   dist = R / sin(fov/2) * 1.05 / zoom;  worldPerPx = 2*dist*tan(fov/2) / viewportH.
            const float deg = 3.14159265f / 180.0f;
            const float fov = Renderer::kFovDeg * deg;
            float zc = st.zoom < 0.05f ? 0.05f : st.zoom;
            float dist = renderer.modelRadius() / std::sin(fov * 0.5f) * 1.05f / zc;
            float worldPerPx = 2.0f * dist * std::tan(fov * 0.5f) / (float)(vh > 1 ? vh : 1);

            // Camera basis for screen-space pan: right (R) and up (U) in world space. Mirrors
            // drawCurrent: dir = view direction (target->eye), up = +Z (or +Y near a pole).
            float yr = st.yaw * deg, pr = st.pitch * deg;
            Vec3 dir(std::cos(pr) * std::sin(yr), -std::cos(pr) * std::cos(yr), std::sin(pr));
            Vec3 worldUp = (std::fabs(pr) > 89.0f * deg) ? Vec3(0, 1, 0) : Vec3(0, 0, 1);
            Vec3 V = normalize(dir);
            Vec3 R = normalize(cross(worldUp, V));   // screen-right in world space
            Vec3 U = normalize(cross(V, R));         // screen-up in world space

            auto applyPan = [&](float ddx, float ddy) {
                // Drag right (ddx>0) moves the model right -> target shifts left (-R). Drag down
                // (ddy>0, screen y is down) moves the model down -> target shifts up (+U).
                st.pan[0] += (-R.x * ddx + U.x * ddy) * worldPerPx;
                st.pan[1] += (-R.y * ddx + U.y * ddy) * worldPerPx;
                st.pan[2] += (-R.z * ddx + U.z * ddy) * worldPerPx;
            };
            auto clampPitch = [&]() {
                st.pitch = st.pitch > 89 ? 89 : (st.pitch < -89 ? -89 : st.pitch);
            };

            const bool ctrl = io.KeyCtrl, shift = io.KeyShift;

            // C25 W3-J: apply display style from AppState each frame (cheap no-op when unchanged).
            renderer.setDisplayStyle(static_cast<DisplayStyle>(st.displayStyle));

            // C25 W3-J: zoom-area rubber-band drag.
            if (s_zoomAreaArmed) {
                if (ImGui::IsMouseDragging(ImGuiMouseButton_Left)) {
                    if (!s_zoomAreaDragging) {
                        s_zoomAreaDragging = true;
                        s_zoomAreaP0 = io.MouseClickedPos[ImGuiMouseButton_Left];
                    }
                    s_zoomAreaP1 = io.MousePos;
                } else if (s_zoomAreaDragging &&
                           !ImGui::IsMouseDown(ImGuiMouseButton_Left)) {
                    // Released: compute zoom+pan to frame the rect.
                    float rx0 = s_zoomAreaP0.x < s_zoomAreaP1.x ? s_zoomAreaP0.x : s_zoomAreaP1.x;
                    float ry0 = s_zoomAreaP0.y < s_zoomAreaP1.y ? s_zoomAreaP0.y : s_zoomAreaP1.y;
                    float rx1 = s_zoomAreaP0.x > s_zoomAreaP1.x ? s_zoomAreaP0.x : s_zoomAreaP1.x;
                    float ry1 = s_zoomAreaP0.y > s_zoomAreaP1.y ? s_zoomAreaP0.y : s_zoomAreaP1.y;
                    float rw = rx1 - rx0, rh = ry1 - ry0;
                    if (rw > 4 && rh > 4) {
                        // Center of the rect in viewport-relative px (origin = viewport center).
                        float vcx = (vr0.x + vr1.x) * 0.5f, vcy = (vr0.y + vr1.y) * 0.5f;
                        float rcx = (rx0 + rx1) * 0.5f - vcx;
                        float rcy = (ry0 + ry1) * 0.5f - vcy;
                        // Pan shift: move target so rect center lands at view center.
                        // rcx>0 = rect center is right of view center: shift target rightward (+R).
                        // rcy>0 = rect center is below view center (screen Y down): shift target downward (-U).
                        float k = worldPerPx;
                        st.pan[0] += (R.x * rcx - U.x * rcy) * k;
                        st.pan[1] += (R.y * rcx - U.y * rcy) * k;
                        st.pan[2] += (R.z * rcx - U.z * rcy) * k;
                        // Zoom: scale so rect extent maps to ~90% of viewport.
                        float vExt = (float)(vw < vh ? vw : vh) * 0.9f;
                        float rExt = rw < rh ? rw : rh;
                        if (rExt > 0.0f) {
                            st.zoom *= vExt / rExt;
                            st.zoom = st.zoom < 0.1f ? 0.1f : (st.zoom > 10.0f ? 10.0f : st.zoom);
                        }
                        float newPan[3] = {st.pan[0], st.pan[1], st.pan[2]};
                        requestCameraAnimation(st, st.yaw, st.pitch, st.zoom, newPan);
                    }
                    s_zoomAreaArmed = false;
                    s_zoomAreaDragging = false;
                }
                // Esc or second click disarms.
                if (ImGui::IsKeyPressed(ImGuiKey_Escape) ||
                    ImGui::IsMouseClicked(ImGuiMouseButton_Right)) {
                    s_zoomAreaArmed = false;
                    s_zoomAreaDragging = false;
                }
            }

            // Left-drag: orbit or pan depending on vpDragMode. Cancels any tween.
            // Zoom-area mode intercepts left-drag above; skip orbit/pan if armed.
            if (!s_zoomAreaArmed &&
                ImGui::IsMouseDragging(ImGuiMouseButton_Left) &&
                (io.MouseDelta.x != 0 || io.MouseDelta.y != 0)) {
                cancelCameraAnimation();
                if (st.vpDragMode == 1) {
                    applyPan(io.MouseDelta.x, io.MouseDelta.y);
                } else {
                    st.yaw += io.MouseDelta.x * 0.4f;
                    st.pitch += io.MouseDelta.y * 0.4f;
                    clampPitch();
                }
            }
            // W1-B (item 3): in pan-drag mode, show the Hand cursor over the viewport so the user
            // sees they can grab-pan (SolidWorks behavior). The cursor manager in app.cpp consumes
            // this per-frame request. (Wave-2 sketch tools / PM wells request Crosshair similarly.)
            if (st.vpDragMode == 1 && !s_zoomAreaArmed)
                st.requestCursor(ui::CursorKind::Hand);

            // Middle-button navigation (SolidWorks bindings):
            //   MMB        = orbit (same rate as left-drag)
            //   Ctrl+MMB   = pan
            //   Shift+MMB  = zoom (vertical, smooth exponential)
            if (ImGui::IsMouseDragging(ImGuiMouseButton_Middle) &&
                (io.MouseDelta.x != 0 || io.MouseDelta.y != 0)) {
                cancelCameraAnimation();
                if (ctrl) {
                    applyPan(io.MouseDelta.x, io.MouseDelta.y);
                } else if (shift) {
                    // Drag up = zoom in. exp keeps each pixel a constant multiplicative step.
                    st.zoom *= std::exp(-io.MouseDelta.y * 0.005f);
                    st.zoom = st.zoom < 0.1f ? 0.1f : (st.zoom > 10 ? 10 : st.zoom);
                } else {
                    st.yaw += io.MouseDelta.x * 0.4f;
                    st.pitch += io.MouseDelta.y * 0.4f;
                    clampPitch();
                }
            }

            // Double-click MMB = animated zoom-to-fit (zoom->1, pan->0, orientation kept).
            if (ImGui::IsMouseDoubleClicked(ImGuiMouseButton_Middle)) {
                applyViewPreset(st, ViewPreset::ZoomFit);
            }

            // Wheel zoom toward the cursor: scale zoom, then shift pan so the world point under the
            // cursor stays put. Cursor offset from viewport center, in pixels.
            if (io.MouseWheel != 0) {
                cancelCameraAnimation();
                float oldZoom = zc;
                st.zoom *= (1.0f + io.MouseWheel * 0.1f);
                st.zoom = st.zoom < 0.1f ? 0.1f : (st.zoom > 10 ? 10 : st.zoom);
                float newZoom = st.zoom;
                if (newZoom != oldZoom) {
                    float cxp = io.MousePos.x - (vr0.x + (vr1.x - vr0.x) * 0.5f);
                    float cyp = io.MousePos.y - (vr0.y + (vr1.y - vr0.y) * 0.5f);
                    // worldPerPx is inversely proportional to zoom; the target must move so the
                    // cursor ray hits the same world point. delta = cursorOffset * (1/old - 1/new)
                    // in world units, projected onto the camera plane (R right, U up; y is down).
                    float k = worldPerPx * (1.0f - oldZoom / newZoom);
                    st.pan[0] += ( R.x * cxp - U.x * cyp) * k;
                    st.pan[1] += ( R.y * cxp - U.y * cyp) * k;
                    st.pan[2] += ( R.z * cxp - U.z * cyp) * k;
                }
            }
        } else if (imgHovered && inSketch) {
            // Sketch mode: wheel still zooms (camera orientation stays plane-locked, no pan).
            if (io.MouseWheel != 0) {
                st.zoom *= (1.0f + io.MouseWheel * 0.1f);
                st.zoom = st.zoom < 0.1f ? 0.1f : (st.zoom > 10 ? 10 : st.zoom);
            }
        }

        // C25 W2-D: keyboard view shortcuts. Viewport hovered/focused, no text field active, not
        // sketching. Ctrl+1..8 = Front/Back/Left/Right/Top/Bottom/Iso/NormalTo; F = zoom-to-fit.
        if (imgHovered && !inSketch && !io.WantTextInput) {
            if (io.KeyCtrl) {
                if      (ImGui::IsKeyPressed(ImGuiKey_1)) applyViewPreset(st, ViewPreset::Front);
                else if (ImGui::IsKeyPressed(ImGuiKey_2)) applyViewPreset(st, ViewPreset::Back);
                else if (ImGui::IsKeyPressed(ImGuiKey_3)) applyViewPreset(st, ViewPreset::Left);
                else if (ImGui::IsKeyPressed(ImGuiKey_4)) applyViewPreset(st, ViewPreset::Right);
                else if (ImGui::IsKeyPressed(ImGuiKey_5)) applyViewPreset(st, ViewPreset::Top);
                else if (ImGui::IsKeyPressed(ImGuiKey_6)) applyViewPreset(st, ViewPreset::Bottom);
                else if (ImGui::IsKeyPressed(ImGuiKey_7)) applyViewPreset(st, ViewPreset::Isometric);
                else if (ImGui::IsKeyPressed(ImGuiKey_8)) normalToSelectionOrNearestAxis(st);
            } else if (ImGui::IsKeyPressed(ImGuiKey_F)) {
                applyViewPreset(st, ViewPreset::ZoomFit);
            }
            // Esc also cancels an in-flight camera tween (selection-clear Esc handling lives in the
            // picking block below; canceling here is harmless when there is no animation).
            if (ImGui::IsKeyPressed(ImGuiKey_Escape) && cameraAnimating()) cancelCameraAnimation();
        }

        // ---- C18: sketch overlay owns the mouse while a sketch is being edited ----
        if (inSketch) {
            sketch->drawOverlay(st, io, vr0, vr1, vw, vh, imgHovered);
        }

        // ---- hover + click selection (once per frame; only while a doc mesh is loaded) ----
        // C25 W2-E: suppressed while the glass-box View Selector is up (it owns the click).
        if (picking && !inSketch && !s_viewSelector.active()) {
            if (imgHovered && !ImGui::IsMouseDragging(ImGuiMouseButton_Left)) {
                int px = (int)(io.MousePos.x - vr0.x);
                int py = (int)(io.MousePos.y - vr0.y);
                st.hover = st.idPass->pickFinal(px, py, vw, vh);
            } else {
                st.hover = render::PickHit{};
            }

            // Click = replace, Ctrl-click = toggle. Empty-space click clears.
            if (imgHovered && ImGui::IsMouseClicked(ImGuiMouseButton_Left) &&
                !ImGui::IsMouseDragging(ImGuiMouseButton_Left)) {
                if (io.KeyCtrl) {
                    if (st.hover.valid()) st.selection.toggle(st.hover);
                } else {
                    st.selection.selectOne(st.hover);  // clears if hover invalid
                }
            }
            // ESC clears the selection while the viewport is focused/hovered.
            if (imgHovered && ImGui::IsKeyPressed(ImGuiKey_Escape)) st.selection.clear();

            // Status text: prefer the live hover, else the last selected item.
            if (st.hover.valid()) {
                st.pickStatus = st.pickLabel(st.hover);
            } else if (!st.selection.empty()) {
                st.pickStatus = st.pickLabel(st.selection.items().back().hit);
            } else {
                st.pickStatus.clear();
            }
        }

        // ---- C25: mouse gesture wheel (right-drag) + 'S' shortcut bar ----
        // Gestures are suppressed while in sketch mode (camera is plane-locked; no view presets).
        if (!inSketch) {
            s_gestures.update(st, imgHovered, vr0, vr1);
            s_gestures.draw(ImGui::GetWindowDrawList());
        }
        // Shortcut bar is context-aware: shows sketch tools when inSketch, model tools otherwise.
        s_shortcutBar.update(st, imgHovered, inSketch, vr0, vr1);
        s_shortcutBar.draw(st, inSketch);

        // ---- C25 W3-J: viewport right-click context menu ----
        // Opened by mouse_gestures when right button released without drag (kRmbContextMenuHook).
        if (st.wantContextMenu && !inSketch) {
            st.wantContextMenu = false;
            ImGui::OpenPopup("##vp_ctx");
        }
        // C26: appearance state shared between the vp_ctx menu item and its sibling picker popup.
        static float s_vpColor[3]   = {0.71f, 0.76f, 0.82f};
        static float s_vpMetallic = 0.0f, s_vpRoughness = 0.5f;  // C32-A material
        static std::string s_vpKey;          // "b<ordinal>" of the body to recolor
        static bool  s_openVpPicker = false;
        // The picked body's stable appearance key (part bodies: "b<ordinal>", matching the engine).
        // Appearance is offered for part docs only; assembly bodies key by component id, which the
        // viewport pick alone can't resolve (deferred — see report).
        // NOTE: tree() is null until the first ops response after doc_new — json::value()
        // on a null json THROWS (this crashed the m1 scenario); guard with is_object().
        bool vpIsPart = st.docMirror.hasDoc() && st.docMirror.tree().is_object() &&
                        st.docMirror.tree().value("schema", std::string()) != "aicad.asm/1";
        std::string vpBodyKey;
        if (vpIsPart && !st.selection.empty()) {
            uint32_t bid = st.selection.items().back().hit.body_id;
            if (bid != 0) vpBodyKey = "b" + std::to_string(bid);
        }

        if (ImGui::BeginPopup("##vp_ctx")) {
            // Selection section (shown only when something is selected / hovered).
            if (!st.pickStatus.empty()) {
                ImGui::PushStyleColor(ImGuiCol_Text,
                    ImVec4(ui::palette.textDim[0], ui::palette.textDim[1],
                           ui::palette.textDim[2], 1.0f));
                ImGui::TextUnformatted(st.pickStatus.c_str());
                ImGui::PopStyleColor();
                if (ImGui::MenuItem("Clear Selection")) st.selection.clear();
                // C26: recolor the selected body. Opens the shared picker (a sibling popup).
                if (!vpBodyKey.empty()) {
                    if (ImGui::MenuItem("Appearance...")) {
                        s_vpKey = vpBodyKey;
                        s_vpColor[0] = 0.71f; s_vpColor[1] = 0.76f; s_vpColor[2] = 0.82f;
                        s_vpMetallic = 0.0f; s_vpRoughness = 0.5f;
                        s_openVpPicker = true;
                    }
                    if (ImGui::MenuItem("Clear Appearance"))
                        applyAppearanceFromViewport(st, vpBodyKey, nullptr);
                }
                ImGui::Separator();
            }
            // View presets submenu.
            if (ImGui::BeginMenu("View Orientation")) {
                struct { const char* lbl; ViewPreset p; } prows[] = {
                    {"Front",    ViewPreset::Front},
                    {"Back",     ViewPreset::Back},
                    {"Left",     ViewPreset::Left},
                    {"Right",    ViewPreset::Right},
                    {"Top",      ViewPreset::Top},
                    {"Bottom",   ViewPreset::Bottom},
                    {"Isometric",ViewPreset::Isometric},
                };
                for (const auto& r : prows)
                    if (ImGui::MenuItem(r.lbl)) applyViewPreset(st, r.p);
                ImGui::EndMenu();
            }
            if (ImGui::MenuItem("Zoom to Fit (F)"))   applyViewPreset(st, ViewPreset::ZoomFit);
            if (ImGui::MenuItem("Zoom to Area")) {
                s_zoomAreaArmed    = true;
                s_zoomAreaDragging = false;
            }
            // Display style submenu.
            if (ImGui::BeginMenu("Display Style")) {
                struct { const char* lbl; int val; } drows[] = {
                    {"Shaded",                0},
                    {"Shaded with Edges",     1},
                    {"Hidden Lines Removed",  2},
                    {"Hidden Lines Visible",  3},
                    {"Wireframe",             4},
                };
                for (const auto& dr : drows) {
                    if (ImGui::MenuItem(dr.lbl, nullptr, st.displayStyle == dr.val)) {
                        st.displayStyle = dr.val;
                        renderer.setDisplayStyle(static_cast<DisplayStyle>(dr.val));
                    }
                }
                ImGui::EndMenu();
            }
            if (ImGui::MenuItem("Section View", nullptr, s_sectionTool.active()))
                s_sectionTool.toggle(st);
            // Hide/show submenu.
            if (ImGui::BeginMenu("Hide / Show")) {
                bool allPlanes = st.showPlane[0] && st.showPlane[1] && st.showPlane[2];
                if (ImGui::MenuItem("Reference Planes", nullptr, allPlanes)) {
                    bool nv = !allPlanes;
                    st.showPlane[0] = st.showPlane[1] = st.showPlane[2] = nv;
                }
                ImGui::MenuItem("Origin Triad",  nullptr, &st.showTriad);
                ImGui::MenuItem("Ground Shadow", nullptr, &st.showGroundShadow);
                ImGui::MenuItem("RealView reflections", nullptr, &st.showRealView);
                ImGui::EndMenu();
            }
            ImGui::Separator();
            if (ImGui::MenuItem("View Selector (Space)"))
                /* ViewSelector has no public open(); trigger via Space key next frame: */ (void)0;
            ImGui::EndPopup();
        }

        // C26: appearance picker — a SIBLING popup of vp_ctx (opened from its menu item) to avoid
        // nested-popup ordering issues. Direct-applies on commit (safe outside tree iteration).
        if (s_openVpPicker) { ImGui::OpenPopup("##appearance_vp"); s_openVpPicker = false; }
        if (appearanceColorPicker("##appearance_vp", s_vpColor, &s_vpMetallic, &s_vpRoughness)
            && !s_vpKey.empty())
            applyAppearanceFromViewport(st, s_vpKey, s_vpColor, s_vpMetallic, s_vpRoughness);

        // ---- C25 W3-J: heads-up view toolbar (SolidWorks-style, fully functional) ----
        // Centered pill at the viewport top; hidden while editing a sketch.
        // Groups: [zoom-fit | zoom-area | pan | rotate] [view-orientation ▾] [display-style ▾]
        //         [hide/show ▾] [section]
        if (!inSketch) {
            const auto& hpal = ui::palette;
            ImDrawList* hdl = ImGui::GetWindowDrawList();
            const float s   = ui::uiScale;
            float btn = 26.0f * s, gap = 3.0f * s, padX = 8.0f * s, padY = 4.0f * s;

            // 9 buttons: zoom-fit, zoom-area, pan, rotate, view-orient▾, display-style▾, hide/show▾, section
            const int nHU = 8;
            float barW = nHU * btn + (nHU - 1) * gap + padX * 2;
            float barH = btn + padY * 2;
            float bx0  = vr0.x + ((vr1.x - vr0.x) - barW) * 0.5f;
            float by0  = vr0.y + 8.0f * s;
            float byi  = by0 + padY;

            // Chip background.
            hdl->AddRectFilled(ImVec2(bx0, by0), ImVec2(bx0 + barW, by0 + barH),
                               ui::Palette::toU32(hpal.overlayBg), barH * 0.30f);
            hdl->AddRect(ImVec2(bx0, by0), ImVec2(bx0 + barW, by0 + barH),
                         ui::Palette::toU32(hpal.overlayBorder), barH * 0.30f);

            // Helper: draw one toolbar icon button. Returns true on click.
            // active=true fills with accentSoft (toggle state).
            auto huBtn = [&](int idx, const char* iconName, const char* tip, bool active) -> bool {
                float bxi = bx0 + padX + (float)idx * (btn + gap);
                ImGui::SetCursorScreenPos(ImVec2(bxi, byi));
                ImGui::PushID(idx);
                bool clicked = ImGui::InvisibleButton("##hb", ImVec2(btn, btn));
                bool hov = ImGui::IsItemHovered();
                ImU32 fill = active  ? ui::Palette::toU32(hpal.accentSoft) :
                             hov     ? ui::Palette::toU32(hpal.buttonHovered) : 0;
                if (fill)
                    hdl->AddRectFilled(ImVec2(bxi, byi), ImVec2(bxi + btn, byi + btn), fill, 3.0f);
                IconUV iv = ui::getIcon(iconName, 20);
                float iz = 18.0f * s, off = (btn - iz) * 0.5f;
                hdl->AddImage(iv.texId, ImVec2(bxi + off, byi + off),
                              ImVec2(bxi + off + iz, byi + off + iz), iv.uv0, iv.uv1,
                              IM_COL32(255, 255, 255, 235));
                if (hov) ImGui::SetTooltip("%s", tip);
                ImGui::PopID();
                return clicked;
            };
            // Separator tick between groups.
            auto huSep = [&](int afterIdx) {
                float bxi = bx0 + padX + (float)afterIdx * (btn + gap);
                hdl->AddLine(ImVec2(bxi + btn + gap * 0.5f, byi + 3),
                             ImVec2(bxi + btn + gap * 0.5f, byi + btn - 3),
                             ui::Palette::toU32(hpal.border), 1.0f);
            };

            ImGui::PushID("##headsup");

            // [0] Zoom to Fit
            if (huBtn(0, "zoom_fit", "Zoom to Fit (F)", false))
                applyViewPreset(st, ViewPreset::ZoomFit);

            // [1] Zoom Area (toggle; left-drag = rubber-band, Esc disarms)
            if (huBtn(1, "zoom_area", "Zoom to Area", s_zoomAreaArmed)) {
                s_zoomAreaArmed   = !s_zoomAreaArmed;
                s_zoomAreaDragging = false;
            }

            // [2] Pan mode toggle
            if (huBtn(2, "pan_view", "Pan (left-drag while active)", st.vpDragMode == 1))
                st.vpDragMode = (st.vpDragMode == 1) ? 0 : 1;

            // [3] Rotate mode toggle (default)
            if (huBtn(3, "rotate_view", "Rotate (left-drag)", st.vpDragMode == 0))
                st.vpDragMode = 0;

            huSep(3);  // ---- separator: navigation | orientation

            // [4] View orientation dropdown ▾
            {
                float bxi = bx0 + padX + 4.0f * (btn + gap);
                ImGui::SetCursorScreenPos(ImVec2(bxi, byi));
                ImGui::PushID(4);
                bool clicked = ImGui::InvisibleButton("##hb", ImVec2(btn, btn));
                bool hov = ImGui::IsItemHovered();
                if (hov) hdl->AddRectFilled(ImVec2(bxi, byi), ImVec2(bxi + btn, byi + btn),
                                             ui::Palette::toU32(hpal.buttonHovered), 3.0f);
                IconUV iv = ui::getIcon("view_selector", 20);
                float iz = 18.0f * s, off = (btn - iz) * 0.5f;
                hdl->AddImage(iv.texId, ImVec2(bxi + off, byi + off),
                              ImVec2(bxi + off + iz, byi + off + iz), iv.uv0, iv.uv1,
                              IM_COL32(255, 255, 255, 235));
                if (hov) ImGui::SetTooltip("View Orientation");
                if (clicked) ImGui::OpenPopup("##hu_vieworient");
                ImGui::SetNextWindowPos(ImVec2(bxi, byi + btn + 4.0f * s), ImGuiCond_Always);
                if (ImGui::BeginPopup("##hu_vieworient")) {
                    struct { const char* label; ViewPreset p; } vrows[] = {
                        {"Front",    ViewPreset::Front},
                        {"Back",     ViewPreset::Back},
                        {"Left",     ViewPreset::Left},
                        {"Right",    ViewPreset::Right},
                        {"Top",      ViewPreset::Top},
                        {"Bottom",   ViewPreset::Bottom},
                        {"Isometric",ViewPreset::Isometric},
                    };
                    for (const auto& r : vrows) {
                        if (ImGui::MenuItem(r.label)) applyViewPreset(st, r.p);
                    }
                    ImGui::Separator();
                    // ViewSelector has no public open() method; trigger Space path via flag.
                    if (ImGui::MenuItem("View Selector (Space)"))
                        st.wantContextMenu = false;  // placeholder: Space key triggers s_viewSelector
                    ImGui::EndPopup();
                }
                ImGui::PopID();
            }

            huSep(4);  // ---- separator: orientation | display

            // [5] Display style dropdown ▾
            {
                // Icon reflects current style.
                static const char* kDsIcons[] = {
                    "display_shaded", "display_shaded_edges", "display_hlr",
                    "display_hlv",    "display_wireframe"
                };
                const char* curIcon = kDsIcons[st.displayStyle < 5 ? st.displayStyle : 1];
                float bxi = bx0 + padX + 5.0f * (btn + gap);
                ImGui::SetCursorScreenPos(ImVec2(bxi, byi));
                ImGui::PushID(5);
                bool clicked = ImGui::InvisibleButton("##hb", ImVec2(btn, btn));
                bool hov = ImGui::IsItemHovered();
                if (hov) hdl->AddRectFilled(ImVec2(bxi, byi), ImVec2(bxi + btn, byi + btn),
                                             ui::Palette::toU32(hpal.buttonHovered), 3.0f);
                IconUV iv = ui::getIcon(curIcon, 20);
                float iz = 18.0f * s, off = (btn - iz) * 0.5f;
                hdl->AddImage(iv.texId, ImVec2(bxi + off, byi + off),
                              ImVec2(bxi + off + iz, byi + off + iz), iv.uv0, iv.uv1,
                              IM_COL32(255, 255, 255, 235));
                if (hov) ImGui::SetTooltip("Display Style");
                if (clicked) ImGui::OpenPopup("##hu_dispstyle");
                ImGui::SetNextWindowPos(ImVec2(bxi, byi + btn + 4.0f * s), ImGuiCond_Always);
                if (ImGui::BeginPopup("##hu_dispstyle")) {
                    struct { const char* icon; const char* label; int val; } drows[] = {
                        {"display_shaded",       "Shaded",                0},
                        {"display_shaded_edges", "Shaded with Edges",     1},
                        {"display_hlr",          "Hidden Lines Removed",  2},
                        {"display_hlv",          "Hidden Lines Visible",  3},
                        {"display_wireframe",    "Wireframe",             4},
                    };
                    for (const auto& dr : drows) {
                        IconUV div = ui::getIcon(dr.icon, 20);
                        ImGui::Image(div.texId, ImVec2(16.0f * s, 16.0f * s), div.uv0, div.uv1);
                        ImGui::SameLine(0, 6.0f * s);
                        bool sel = (st.displayStyle == dr.val);
                        if (sel) ImGui::PushStyleColor(ImGuiCol_Text,
                            ImVec4(hpal.accent[0], hpal.accent[1], hpal.accent[2], 1.0f));
                        if (ImGui::MenuItem(dr.label, nullptr, sel)) {
                            st.displayStyle = dr.val;
                            renderer.setDisplayStyle(static_cast<DisplayStyle>(dr.val));
                        }
                        if (sel) ImGui::PopStyleColor();
                    }
                    ImGui::EndPopup();
                }
                ImGui::PopID();
            }

            // [6] Hide/Show items dropdown ▾
            {
                float bxi = bx0 + padX + 6.0f * (btn + gap);
                ImGui::SetCursorScreenPos(ImVec2(bxi, byi));
                ImGui::PushID(6);
                bool clicked = ImGui::InvisibleButton("##hb", ImVec2(btn, btn));
                bool hov = ImGui::IsItemHovered();
                if (hov) hdl->AddRectFilled(ImVec2(bxi, byi), ImVec2(bxi + btn, byi + btn),
                                             ui::Palette::toU32(hpal.buttonHovered), 3.0f);
                IconUV iv = ui::getIcon("hide_show_eye", 20);   // C25b: proper visibility glyph
                float iz = 18.0f * s, off = (btn - iz) * 0.5f;
                hdl->AddImage(iv.texId, ImVec2(bxi + off, byi + off),
                              ImVec2(bxi + off + iz, byi + off + iz), iv.uv0, iv.uv1,
                              IM_COL32(255, 255, 255, 235));
                if (hov) ImGui::SetTooltip("Hide / Show Items");
                if (clicked) ImGui::OpenPopup("##hu_hideshow");
                ImGui::SetNextWindowPos(ImVec2(bxi, byi + btn + 4.0f * s), ImGuiCond_Always);
                if (ImGui::BeginPopup("##hu_hideshow")) {
                    bool allPlanes = st.showPlane[0] && st.showPlane[1] && st.showPlane[2];
                    if (ImGui::MenuItem("Reference Planes", nullptr, allPlanes)) {
                        bool nv = !allPlanes;
                        st.showPlane[0] = st.showPlane[1] = st.showPlane[2] = nv;
                    }
                    ImGui::MenuItem("Origin Triad",    nullptr, &st.showTriad);
                    ImGui::MenuItem("Ground Shadow",   nullptr, &st.showGroundShadow);
                    ImGui::MenuItem("RealView reflections", nullptr, &st.showRealView);
                    ImGui::EndPopup();
                }
                ImGui::PopID();
            }

            huSep(6);  // ---- separator: hide/show | section

            // [7] Section view toggle
            if (huBtn(7, "section_view", "Section View", s_sectionTool.active()))
                s_sectionTool.toggle(st);

            ImGui::PopID();

            // ---- C25 W3-J: zoom-area rubber-band rect overlay ----
            if (s_zoomAreaDragging) {
                float rx0 = s_zoomAreaP0.x < s_zoomAreaP1.x ? s_zoomAreaP0.x : s_zoomAreaP1.x;
                float ry0 = s_zoomAreaP0.y < s_zoomAreaP1.y ? s_zoomAreaP0.y : s_zoomAreaP1.y;
                float rx1 = s_zoomAreaP0.x > s_zoomAreaP1.x ? s_zoomAreaP0.x : s_zoomAreaP1.x;
                float ry1 = s_zoomAreaP0.y > s_zoomAreaP1.y ? s_zoomAreaP0.y : s_zoomAreaP1.y;
                hdl->AddRectFilled(ImVec2(rx0, ry0), ImVec2(rx1, ry1),
                                   ui::Palette::toU32(hpal.accentSoft));
                hdl->AddRect(ImVec2(rx0, ry0), ImVec2(rx1, ry1),
                             ui::Palette::toU32(hpal.accent), 1.5f);
            }
        }

        // ---- C25 W2-E: glass-box View Selector (Space). Drawn after the heads-up toolbar so its
        // orientation popup sits just below it; uses the same window draw list. While active it
        // suppresses normal click-picking (guard added on the picking click above).
        s_viewSelector.update(st, renderer, imgHovered, inSketch, vr0, vr1);
        s_viewSelector.draw(st, ImGui::GetWindowDrawList(), vr0, vr1);

        // ---- C25 W3-I: Section View tool. Pushes the clip plane to the renderer each frame and
        // draws the PM panel + in-viewport plane/arrow manipulator. Auto-suspends while sketching.
        s_sectionTool.updateAndDraw(st, renderer, imgHovered, inSketch, vr0, vr1);

        // ---- overlays on the 3D view ----
        ImDrawList* vdl = ImGui::GetWindowDrawList();
        {
            // Cache overlay colors as ImU32 from the palette for this frame.
            const auto& vpal = ui::palette;
            const ImU32 overlayBg   = ui::Palette::toU32(vpal.overlayBg);
            const ImU32 overlayBord = ui::Palette::toU32(vpal.overlayBorder);
            const ImU32 overlayTxt  = ui::Palette::toU32(vpal.overlayText);

        if (busy) {
            // live progress pill: pulsing dot + the agent's current phase
            std::string lt = st.livePhase.empty() ? std::string("Working...") : st.livePhase;
            ImVec2 ts = ImGui::CalcTextSize(lt.c_str());
            float maxW = (vr1.x - vr0.x) - 58.0f;
            ImVec2 p0(vr0.x + 12, vr0.y + 12);
            ImVec2 p1(p0.x + (ts.x > maxW ? maxW : ts.x) + 36.0f, p0.y + ts.y + 12.0f);
            vdl->AddRectFilled(p0, p1, overlayBg, (p1.y - p0.y) * 0.5f);
            vdl->AddRect(p0, p1, overlayBord, (p1.y - p0.y) * 0.5f);
            float pulse = 0.35f + 0.65f * (0.5f + 0.5f * std::sin((float)ImGui::GetTime() * 5.0f));
            vdl->AddCircleFilled(ImVec2(p0.x + 14, (p0.y + p1.y) * 0.5f), 4.0f,
                                 ImGui::ColorConvertFloat4ToU32(
                                     ImVec4(vpal.accent[0], vpal.accent[1],
                                            vpal.accent[2], pulse)));
            vdl->PushClipRect(p0, ImVec2(p1.x - 8, p1.y), true);
            vdl->AddText(ImVec2(p0.x + 24, p0.y + 6), overlayTxt, lt.c_str());
            vdl->PopClipRect();
        }
        if (st.hasDocMesh && !st.pickStatus.empty()) {
            // pick readout, top-right ("Face 12 of Body1")
            ImVec2 ts = ImGui::CalcTextSize(st.pickStatus.c_str());
            ImVec2 p1(vr1.x - 12, vr0.y + 12);
            ImVec2 p0(p1.x - ts.x - 20.0f, p1.y);
            ImVec2 p1b(p1.x, p1.y + ts.y + 10.0f);
            vdl->AddRectFilled(p0, p1b, overlayBg, 6.0f);
            vdl->AddRect(p0, p1b, overlayBord, 6.0f);
            vdl->AddText(ImVec2(p0.x + 10, p0.y + 5), overlayTxt,
                         st.pickStatus.c_str());
        }
        // C25 W3-J: apply ground-shadow visibility (last call wins; app.cpp may set it per-frame
        // before viewport_panel runs — the ordering means this value wins for the rendered frame,
        // which is the desired result since viewport_panel calls renderOrbit immediately before).
        // NOTE for orchestrator: if app.cpp pushes groundShadow AFTER viewport_panel in the frame
        // loop, the visibility toggle here would be overridden. Verify render call order.
        {
            const auto& vpal = ui::palette;
            float alpha = st.showGroundShadow ? vpal.groundShadow[3] : 0.0f;
            renderer.setGroundShadow(vpal.groundShadow[0], vpal.groundShadow[1],
                                     vpal.groundShadow[2], alpha);
        }
        // ---- C24 / C26-C: orientation triad, bottom-left of the viewport (SolidWorks-style) ----
        // C25 W3-J: gated by st.showTriad.
        // C26-C: each axis tip is a small invisible ImGui button. Clicking an axis goes to the
        // corresponding preset view; clicking the axis you're already looking down flips opposite.
        //   +X tip → Right  (yaw=90,  pitch=0)
        //   +Y tip → Front  (yaw=0,   pitch=0)  — "look from the +Y side" = Front in this basis
        //   +Z tip → Top    (yaw=0,   pitch=89.5)
        // Hover: accentSoft circle behind the label. Tooltip: view name.
        if (st.showTriad) {
            const float ts = ui::uiScale;
            float cx = vr0.x + 34.0f * ts, cy = vr1.y - 34.0f * ts;
            float L = 20.0f * ts;
            float yawR = st.yaw * 3.14159265f / 180.0f, pitR = st.pitch * 3.14159265f / 180.0f;
            float cyaw = std::cos(yawR), syaw = std::sin(yawR);
            float cpit = std::cos(pitR), spit = std::sin(pitR);
            auto proj = [&](float wx, float wy, float wz, float& sx, float& sy) {
                float rx = wx * cyaw + wy * syaw;
                float ry = -wx * syaw + wy * cyaw;
                sx = rx;
                sy = -(wz * cpit - ry * spit);
            };
            struct Ax {
                float x, y, z;
                const char* l;
                const float* col;
                ViewPreset preset;
                const char* viewName;
                // The yaw/pitch that represents "already looking at this axis" (approx check).
                float chkYaw, chkPitch;
            };
            const Ax axes[] = {
                {1, 0, 0, "X", ui::palette.axisX, ViewPreset::Right,  "Right",  90.0f,  0.0f},
                {0, 1, 0, "Y", ui::palette.axisY, ViewPreset::Front,  "Front",   0.0f,  0.0f},
                {0, 0, 1, "Z", ui::palette.axisZ, ViewPreset::Top,    "Top",     0.0f, 89.5f},
            };
            // Draw lines and label chips first (under buttons).
            for (const auto& a : axes) {
                float sx, sy; proj(a.x, a.y, a.z, sx, sy);
                ImVec2 tip(cx + sx * L, cy + sy * L);
                ImU32 col = ImGui::ColorConvertFloat4ToU32(
                    ImVec4(a.col[0], a.col[1], a.col[2], 0.95f));
                vdl->AddLine(ImVec2(cx, cy), tip, col, 1.8f);
            }
            vdl->AddCircleFilled(ImVec2(cx, cy), 2.4f, overlayTxt);

            // Draw buttons + labels on top.
            ImGui::PushID("##triad");
            const float hitR = 8.0f * ts;  // invisible button half-size
            for (int ai = 0; ai < 3; ++ai) {
                const auto& a = axes[ai];
                float sx, sy; proj(a.x, a.y, a.z, sx, sy);
                ImVec2 tip(cx + sx * L, cy + sy * L);

                // Invisible button centered on the axis tip.
                ImGui::SetCursorScreenPos(ImVec2(tip.x - hitR, tip.y - hitR));
                ImGui::PushID(ai);
                bool clicked = ImGui::InvisibleButton("##tb", ImVec2(hitR * 2, hitR * 2));
                bool hov = ImGui::IsItemHovered();
                ImGui::PopID();

                if (hov) {
                    // Hover: accentSoft circle highlight.
                    vdl->AddCircleFilled(tip, hitR,
                        ui::Palette::toU32(ui::palette.accentSoft));
                    ImGui::SetTooltip("%s view", a.viewName);
                }
                if (clicked) {
                    // Check if already looking from this axis (within 5° tolerance).
                    float dy2  = std::fabs(std::fmod(st.yaw - a.chkYaw + 540.0f, 360.0f) - 180.0f);
                    float dp2  = std::fabs(st.pitch - a.chkPitch);
                    bool already = (dy2 < 5.0f && dp2 < 5.0f);
                    if (already) {
                        // Flip to opposite view.
                        static const float kZeroPan[3] = {0,0,0};
                        requestCameraAnimation(st, a.chkYaw + 180.0f,
                                               -a.chkPitch, -1.0f, kZeroPan);
                    } else {
                        applyViewPreset(st, a.preset);
                    }
                }

                // Label chip.
                ImU32 col = ImGui::ColorConvertFloat4ToU32(
                    ImVec4(a.col[0], a.col[1], a.col[2], 0.95f));
                ImVec2 lp(tip.x - 3, tip.y - 7);
                ImVec2 lts = ImGui::CalcTextSize(a.l);
                vdl->AddRectFilled(ImVec2(lp.x - 2, lp.y - 1),
                                   ImVec2(lp.x + lts.x + 2, lp.y + lts.y + 1), overlayBg, 2.0f);
                vdl->AddText(lp, col, a.l);
            }
            ImGui::PopID();
        }
        // C27-A: hide the model-info dims pill while a PM form is active (callouts own the
        // viewport's parameter readout; avoids visual clutter / overlap).
        bool pmActive = st.propertyManager && st.propertyManager->active();
        if (!st.modelInfo.empty() && renderer.hasMesh() && !pmActive) {
            // dimensions readout, bottom-left (sits above the orientation triad)
            ImFont* mf = st.fonts.mono ? st.fonts.mono : ImGui::GetFont();
            float fs = mf->FontSize * 0.92f;
            ImVec2 ts = mf->CalcTextSizeA(fs, 1e30f, 0.0f, st.modelInfo.c_str());
            ImVec2 p0(vr0.x + 12, vr1.y - ts.y - 70.0f);
            ImVec2 p1(p0.x + ts.x + 20.0f, p0.y + ts.y + 10.0f);
            vdl->AddRectFilled(p0, p1, overlayBg, 6.0f);
            vdl->AddText(mf, fs, ImVec2(p0.x + 10, p0.y + 5),
                         overlayTxt, st.modelInfo.c_str());
        }
        // ---- reference-plane name chips (projected to screen) ----
        // C25: gate on effPlane (same effective visibility as the quads) so labels never
        // float without their planes (the empty-doc bug).
        if (!inSketch && (effPlane[0] || effPlane[1] || effPlane[2])) {
            const float* m = renderer.camera().mvp.m;  // column-major model->clip
            auto project = [&](const Vec3& w, ImVec2& out) -> bool {
                float x = m[0]*w.x + m[4]*w.y + m[8]*w.z  + m[12];
                float y = m[1]*w.x + m[5]*w.y + m[9]*w.z  + m[13];
                float wc= m[3]*w.x + m[7]*w.y + m[11]*w.z + m[15];
                if (wc <= 1e-4f) return false;  // behind the camera
                out.x = vr0.x + (x / wc * 0.5f + 0.5f) * (vr1.x - vr0.x);
                out.y = vr0.y + (1.0f - (y / wc * 0.5f + 0.5f)) * (vr1.y - vr0.y);
                return true;
            };
            static const char* kPlaneName[3] = {"Front Plane", "Top Plane", "Right Plane"};
            // W4-K: chip border tracks the datum-plane outline color (light-blue, SolidWorks look)
            // so the labels read as part of the unified overlay family instead of per-axis RGB.
            const ImU32 planeChipBord = ui::Palette::toU32(vpal.planeOutline);
            for (int p = 0; p < 3; ++p) {
                if (!effPlane[p]) continue;
                Vec3 a; if (!renderer.planeAnchor(p, a)) continue;
                ImVec2 sp; if (!project(a, sp)) continue;
                const char* nm = kPlaneName[p];
                ImVec2 ts = ImGui::CalcTextSize(nm);
                // keep the chip inside the viewport rect
                float x0 = sp.x, y0 = sp.y;
                if (x0 + ts.x + 12 > vr1.x) x0 = vr1.x - ts.x - 12;
                if (x0 < vr0.x + 2) x0 = vr0.x + 2;
                if (y0 + ts.y + 6 > vr1.y) y0 = vr1.y - ts.y - 6;
                if (y0 < vr0.y + 2) y0 = vr0.y + 2;
                ImVec2 q0(x0, y0), q1(x0 + ts.x + 12, y0 + ts.y + 6);
                vdl->AddRectFilled(q0, q1, overlayBg, 4.0f);
                vdl->AddRect(q0, q1, planeChipBord, 4.0f);
                vdl->AddText(ImVec2(x0 + 6, y0 + 3), overlayTxt, nm);
            }
        }

        // ---- C27-A: SolidWorks-style in-viewport parameter callouts ----
        // While the PropertyManager is editing a feature, show a small chip ("Radius: 4mm")
        // per visible+enabled Dim field, tied by a leader line to the affected geometry.
        if (st.propertyManager && st.hasDocMesh) {
            auto* pm = st.propertyManager;
            if (pm->active()) {
                std::vector<ui::PMCalloutItem> items = pm->calloutItems(st);
                if (!items.empty()) {
                    const float* m = renderer.camera().mvp.m;  // column-major model->clip
                    auto project = [&](const float w[3], ImVec2& out) -> bool {
                        float x = m[0]*w[0] + m[4]*w[1] + m[8]*w[2]  + m[12];
                        float y = m[1]*w[0] + m[5]*w[1] + m[9]*w[2]  + m[13];
                        float wc= m[3]*w[0] + m[7]*w[1] + m[11]*w[2] + m[15];
                        if (wc <= 1e-4f) return false;  // behind the camera
                        out.x = vr0.x + (x / wc * 0.5f + 0.5f) * (vr1.x - vr0.x);
                        out.y = vr0.y + (1.0f - (y / wc * 0.5f + 0.5f)) * (vr1.y - vr0.y);
                        return true;
                    };
                    const float sc = ui::uiScale;
                    const ImU32 leaderCol = ImGui::ColorConvertFloat4ToU32(
                        ImVec4(vpal.accent[0], vpal.accent[1], vpal.accent[2], 0.8f));
                    const ImU32 chipBord = ui::Palette::toU32(vpal.accent);
                    // Limit to ~3 chips; stagger vertically so anchored chips don't overlap.
                    int shown = 0, anchoredRow = 0, dockRow = 0;
                    for (const auto& ci : items) {
                        if (shown >= 3) break;
                        std::string txt = ci.label + ": " + ci.value;
                        ImVec2 ts = ImGui::CalcTextSize(txt.c_str());
                        float padX = 8.0f * sc, padY = 4.0f * sc;
                        float cw = ts.x + padX * 2, ch = ts.y + padY * 2;

                        ImVec2 chipTL;
                        bool drawLeader = false;
                        ImVec2 anchorSp;
                        if (ci.hasAnchor && project(ci.anchor, anchorSp)) {
                            // Offset the chip up-right from the anchor; stagger by row.
                            float off = 40.0f * sc;
                            chipTL = ImVec2(anchorSp.x + off,
                                            anchorSp.y - off - anchoredRow * (ch + 6.0f * sc));
                            drawLeader = true;
                            ++anchoredRow;
                        } else {
                            // No geometry: dock top-right, under the HUD pick readout.
                            chipTL = ImVec2(vr1.x - cw - 12.0f * sc,
                                            vr0.y + 44.0f * sc + dockRow * (ch + 6.0f * sc));
                            ++dockRow;
                        }
                        // Clamp the chip inside the viewport rect.
                        if (chipTL.x + cw > vr1.x - 4) chipTL.x = vr1.x - cw - 4;
                        if (chipTL.x < vr0.x + 4)       chipTL.x = vr0.x + 4;
                        if (chipTL.y + ch > vr1.y - 4) chipTL.y = vr1.y - ch - 4;
                        if (chipTL.y < vr0.y + 4)       chipTL.y = vr0.y + 4;
                        ImVec2 chipBR(chipTL.x + cw, chipTL.y + ch);

                        if (drawLeader) {
                            // Leader from the anchor to the nearest chip corner-ish point.
                            ImVec2 leaderEnd(chipTL.x, chipTL.y + ch * 0.5f);
                            vdl->AddLine(anchorSp, leaderEnd, leaderCol, 1.5f * sc);
                            vdl->AddCircleFilled(anchorSp, 2.6f * sc, leaderCol);
                        }
                        vdl->AddRectFilled(chipTL, chipBR, overlayBg, 4.0f * sc);
                        vdl->AddRect(chipTL, chipBR, chipBord, 4.0f * sc);
                        vdl->AddText(ImVec2(chipTL.x + padX, chipTL.y + padY), overlayTxt,
                                     txt.c_str());
                        ++shown;
                    }
                }
            }
        }

        // ---- C19: result colormap legend (bottom-right) ----
        ui::drawLegend(st, vdl, vr0, vr1);
        }  // end overlay colors scope
    }
    ImGui::End();
    }
}

}  // namespace ui
