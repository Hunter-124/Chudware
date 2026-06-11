// AI-CAD Viewport panel — 3D orbit view + parts list + fit readout + overlays.
#include <cmath>
#include <string>

#include "imgui.h"

#include "viewport_panel.h"

#include "legend.h"
#include "icons.h"

#include "../core/app_state.h"

#include "../renderer.h"
#include "../render/id_pass.h"
#include "../render/overlays.h"

#include "sketch_mode.h"   // C18

namespace ui {

void drawViewportPanel(AppState& st, ImGuiIO& io, bool busy) {
    Renderer& renderer = *st.renderer;
    // C18: while a sketch is being edited the camera locks normal to the sketch plane and the
    // sketch tools own the mouse (no orbit, no topology picking).
    auto* sketch = static_cast<ui::SketchMode*>(st.sketchMode);
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
        renderer.renderOrbit(st.yaw, st.pitch, st.zoom);

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
        if (imgHovered) {
            // C18: no orbit while sketching (camera is plane-locked); wheel still zooms.
            if (!inSketch && ImGui::IsMouseDragging(ImGuiMouseButton_Left)) {
                st.yaw += io.MouseDelta.x * 0.4f;
                st.pitch += io.MouseDelta.y * 0.4f;
                st.pitch = st.pitch > 89 ? 89 : (st.pitch < -89 ? -89 : st.pitch);
            }
            if (io.MouseWheel != 0) {
                st.zoom *= (1.0f + io.MouseWheel * 0.1f);
                st.zoom = st.zoom < 0.1f ? 0.1f : (st.zoom > 10 ? 10 : st.zoom);
            }
        }

        // ---- C18: sketch overlay owns the mouse while a sketch is being edited ----
        if (inSketch) {
            sketch->drawOverlay(st, io, vr0, vr1, vw, vh, imgHovered);
        }

        // ---- hover + click selection (once per frame; only while a doc mesh is loaded) ----
        if (picking && !inSketch) {
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

        // ---- C24: heads-up view toolbar, centered at the viewport top (SolidWorks-style) ----
        // A translucent chip hosting: Zoom-fit, the orientation presets (Iso/Front/Right/Top),
        // display-style and section toggles. Buttons are InvisibleButtons positioned over the 3D
        // image with hover fills + icon glyphs from the atlas. Hidden while editing a sketch.
        if (!inSketch) {
            const auto& hpal = ui::palette;
            ImDrawList* hdl = ImGui::GetWindowDrawList();
            struct HU { const char* icon; const char* tip; int act; float y, p; };
            // act: 0=zoom-fit(reset), 1=iso,2=front,3=right,4=top, 5=display(noop), 6=section(noop)
            const HU items[] = {
                {"zoom_fit",        "Zoom to Fit",   0, 0, 0},
                {"view_cube",       "Isometric",     1, 35, 28},
                {"orientation",     "Front",         2, 0, 0},
                {"orientation",     "Right",         3, 90, 0},
                {"orientation",     "Top",           4, 0, 89},
                {"display_shaded",  "Display Style", 5, 0, 0},
                {"section_view",    "Section View",  6, 0, 0},
            };
            const int nHU = (int)(sizeof(items) / sizeof(items[0]));
            float btn = 26.0f, gap = 3.0f, padX = 8.0f, padY = 4.0f;
            float barW = nHU * btn + (nHU - 1) * gap + padX * 2;
            float barH = btn + padY * 2;
            float bx0 = vr0.x + ((vr1.x - vr0.x) - barW) * 0.5f;
            float by0 = vr0.y + 8.0f;
            // chip background
            hdl->AddRectFilled(ImVec2(bx0, by0), ImVec2(bx0 + barW, by0 + barH),
                               ui::Palette::toU32(hpal.overlayBg), barH * 0.30f);
            hdl->AddRect(ImVec2(bx0, by0), ImVec2(bx0 + barW, by0 + barH),
                         ui::Palette::toU32(hpal.overlayBorder), barH * 0.30f);
            ImGui::PushID("##headsup");
            for (int i = 0; i < nHU; ++i) {
                float bxi = bx0 + padX + i * (btn + gap);
                float byi = by0 + padY;
                ImGui::SetCursorScreenPos(ImVec2(bxi, byi));
                ImGui::PushID(i);
                bool clicked = ImGui::InvisibleButton("##hb", ImVec2(btn, btn));
                bool hov = ImGui::IsItemHovered();
                if (hov)
                    hdl->AddRectFilled(ImVec2(bxi, byi), ImVec2(bxi + btn, byi + btn),
                                       ui::Palette::toU32(hpal.buttonHovered), 3.0f);
                IconUV iv = ui::getIcon(items[i].icon, 20);
                float iz = 18.0f, off = (btn - iz) * 0.5f;
                hdl->AddImage(iv.texId, ImVec2(bxi + off, byi + off),
                              ImVec2(bxi + off + iz, byi + off + iz), iv.uv0, iv.uv1,
                              IM_COL32(255, 255, 255, 235));
                if (hov) ImGui::SetTooltip("%s", items[i].tip);
                if (clicked) {
                    int a = items[i].act;
                    if (a == 0) { st.zoom = 1.0f; }                    // zoom-fit (reset zoom)
                    else if (a >= 1 && a <= 4) { st.yaw = items[i].y; st.pitch = items[i].p; }
                    // a==5/6 (display style / section) are visual stubs — no functional change.
                }
                ImGui::PopID();
                // small separators before display-style and section groups
                if (i == 0 || i == 4)
                    hdl->AddLine(ImVec2(bxi + btn + gap * 0.5f, byi + 3),
                                 ImVec2(bxi + btn + gap * 0.5f, byi + btn - 3),
                                 ui::Palette::toU32(hpal.border), 1.0f);
            }
            ImGui::PopID();
        }

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
        // ---- C24: orientation triad, bottom-left of the viewport (SolidWorks-style) ----
        // A tiny XYZ gizmo reflecting the current orbit (yaw/pitch). Axes are projected from world
        // to screen with the same convention the renderer uses, so it tracks the live camera.
        {
            float cx = vr0.x + 34.0f, cy = vr1.y - 34.0f;  // triad center, bottom-left
            float L = 20.0f;
            float yawR = st.yaw * 3.14159265f / 180.0f, pitR = st.pitch * 3.14159265f / 180.0f;
            float cyaw = std::cos(yawR), syaw = std::sin(yawR);
            float cpit = std::cos(pitR), spit = std::sin(pitR);
            // World axis -> screen (x right, y down). Project (Xw,Yw,Zw) after yaw about up then
            // pitch: screen_x = Xw*cyaw - Yw*syaw; depth-tilt folds Z up by pitch.
            auto proj = [&](float wx, float wy, float wz, float& sx, float& sy) {
                float rx = wx * cyaw + wy * syaw;
                float ry = -wx * syaw + wy * cyaw;
                sx = rx;
                sy = -(wz * cpit - ry * spit);
            };
            struct Ax { float x, y, z; const char* l; const float* col; };
            const Ax axes[] = {
                {1, 0, 0, "X", ui::palette.axisX},
                {0, 1, 0, "Y", ui::palette.axisY},
                {0, 0, 1, "Z", ui::palette.axisZ},
            };
            for (const auto& a : axes) {
                float sx, sy; proj(a.x, a.y, a.z, sx, sy);
                ImVec2 tip(cx + sx * L, cy + sy * L);
                ImU32 col = ImGui::ColorConvertFloat4ToU32(
                    ImVec4(a.col[0], a.col[1], a.col[2], 0.95f));
                vdl->AddLine(ImVec2(cx, cy), tip, col, 1.8f);
                vdl->AddText(ImVec2(tip.x - 3, tip.y - 7), col, a.l);
            }
            vdl->AddCircleFilled(ImVec2(cx, cy), 2.4f, overlayTxt);
        }
        if (!st.modelInfo.empty() && renderer.hasMesh()) {
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
        // ---- C19: result colormap legend (bottom-right) ----
        ui::drawLegend(st, vdl, vr0, vr1);
        }  // end overlay colors scope
    }
    ImGui::End();
    }
}

}  // namespace ui
