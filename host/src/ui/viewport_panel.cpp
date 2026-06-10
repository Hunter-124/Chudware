// AI-CAD Viewport panel — 3D orbit view + parts list + fit readout + overlays.
#include <cmath>
#include <string>

#include "imgui.h"

#include "viewport_panel.h"

#include "../core/app_state.h"

#include "../renderer.h"
#include "../render/id_pass.h"
#include "../render/overlays.h"

namespace ui {

void drawViewportPanel(AppState& st, ImGuiIO& io, bool busy) {
    Renderer& renderer = *st.renderer;

    // ---------- Viewport ----------
    if (st.showViewport) {
    if (ImGui::Begin("Viewport"))
    {
        struct V { const char* l; float y, p; };
        const V views[] = {{"Iso", 35, 28}, {"Front", 0, 0}, {"Right", 90, 0}, {"Top", 0, 89}};
        for (int i = 0; i < 4; ++i) {
            if (i) ImGui::SameLine();
            if (ImGui::Button(views[i].l)) { st.yaw = views[i].y; st.pitch = views[i].p; }
        }
        ImGui::SameLine();
        if (ImGui::Button("Reset")) { st.yaw = 35; st.pitch = 28; st.zoom = 1.0f; }

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
            if (ImGui::IsMouseDragging(ImGuiMouseButton_Left)) {
                st.yaw += io.MouseDelta.x * 0.4f;
                st.pitch += io.MouseDelta.y * 0.4f;
                st.pitch = st.pitch > 89 ? 89 : (st.pitch < -89 ? -89 : st.pitch);
            }
            if (io.MouseWheel != 0) {
                st.zoom *= (1.0f + io.MouseWheel * 0.1f);
                st.zoom = st.zoom < 0.1f ? 0.1f : (st.zoom > 10 ? 10 : st.zoom);
            }
        }

        // ---- hover + click selection (once per frame; only while a doc mesh is loaded) ----
        if (picking) {
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
        if (!st.modelInfo.empty() && renderer.hasMesh()) {
            // dimensions readout, bottom-left
            ImFont* mf = st.fonts.mono ? st.fonts.mono : ImGui::GetFont();
            float fs = mf->FontSize * 0.92f;
            ImVec2 ts = mf->CalcTextSizeA(fs, 1e30f, 0.0f, st.modelInfo.c_str());
            ImVec2 p0(vr0.x + 12, vr1.y - ts.y - 22.0f);
            ImVec2 p1(p0.x + ts.x + 20.0f, p0.y + ts.y + 10.0f);
            vdl->AddRectFilled(p0, p1, overlayBg, 6.0f);
            vdl->AddText(mf, fs, ImVec2(p0.x + 10, p0.y + 5),
                         overlayTxt, st.modelInfo.c_str());
        }
        }  // end overlay colors scope
    }
    ImGui::End();
    }
}

}  // namespace ui
