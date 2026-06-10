// AI-CAD Viewport panel — 3D orbit view + parts list + fit readout + overlays.
#include <cmath>
#include <string>

#include "imgui.h"

#include "viewport_panel.h"

#include "../core/app_state.h"

#include "../renderer.h"

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
            ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(0.62f, 0.66f, 0.72f, 1));
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
                    ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(0.50f, 0.54f, 0.60f, 1));
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
        ImGui::Image((ImTextureID)(intptr_t)renderer.colorTexture(), ImVec2((float)vw, (float)vh),
                     ImVec2(0, 1), ImVec2(1, 0));
        ImVec2 vr0 = ImGui::GetItemRectMin(), vr1 = ImGui::GetItemRectMax();
        if (ImGui::IsItemHovered()) {
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

        // ---- overlays on the 3D view ----
        ImDrawList* vdl = ImGui::GetWindowDrawList();
        if (busy) {
            // live progress pill: pulsing dot + the agent's current phase
            std::string lt = st.livePhase.empty() ? std::string("Working...") : st.livePhase;
            ImVec2 ts = ImGui::CalcTextSize(lt.c_str());
            float maxW = (vr1.x - vr0.x) - 58.0f;
            ImVec2 p0(vr0.x + 12, vr0.y + 12);
            ImVec2 p1(p0.x + (ts.x > maxW ? maxW : ts.x) + 36.0f, p0.y + ts.y + 12.0f);
            vdl->AddRectFilled(p0, p1, IM_COL32(10, 14, 19, 215), (p1.y - p0.y) * 0.5f);
            vdl->AddRect(p0, p1, IM_COL32(45, 212, 191, 70), (p1.y - p0.y) * 0.5f);
            float pulse = 0.35f + 0.65f * (0.5f + 0.5f * std::sin((float)ImGui::GetTime() * 5.0f));
            vdl->AddCircleFilled(ImVec2(p0.x + 14, (p0.y + p1.y) * 0.5f), 4.0f,
                                 ImGui::ColorConvertFloat4ToU32(
                                     ImVec4(0.176f, 0.831f, 0.749f, pulse)));
            vdl->PushClipRect(p0, ImVec2(p1.x - 8, p1.y), true);
            vdl->AddText(ImVec2(p0.x + 24, p0.y + 6), IM_COL32(226, 232, 240, 240), lt.c_str());
            vdl->PopClipRect();
        }
        if (!st.modelInfo.empty() && renderer.hasMesh()) {
            // dimensions readout, bottom-left
            ImFont* mf = st.fonts.mono ? st.fonts.mono : ImGui::GetFont();
            float fs = mf->FontSize * 0.92f;
            ImVec2 ts = mf->CalcTextSizeA(fs, 1e30f, 0.0f, st.modelInfo.c_str());
            ImVec2 p0(vr0.x + 12, vr1.y - ts.y - 22.0f);
            ImVec2 p1(p0.x + ts.x + 20.0f, p0.y + ts.y + 10.0f);
            vdl->AddRectFilled(p0, p1, IM_COL32(10, 14, 19, 185), 6.0f);
            vdl->AddText(mf, fs, ImVec2(p0.x + 10, p0.y + 5),
                         IM_COL32(176, 186, 198, 235), st.modelInfo.c_str());
        }
    }
    ImGui::End();
    }
}

}  // namespace ui
