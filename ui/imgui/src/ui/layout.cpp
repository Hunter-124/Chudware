// AI-CAD2 dock layout — ported from host/src/ui/layout.cpp (buildDefaultLayout) + glass splitters.
#include "ui/layout.h"

#include "theme/theme.h"
#include "theme/tokens.h"

#include "imgui.h"
#include "imgui_internal.h"

#include <algorithm>

// C24 / SolidWorks 2025 default arrangement (host/src/ui/layout.cpp).
//   LEFT   = Browser task pane (~360px @1x, clamped in pixels).
//   RIGHT  = Assistant task pane (~380px) + lower slot for evaluate/assembly dialogs.
//   CENTER = Viewport (as large as possible; ECAD/CAM tabs pre-registered for later waves).
void buildDefaultLayout(ImGuiID root, const ImVec2& size) {
    ImGui::DockBuilderRemoveNode(root);
    ImGui::DockBuilderAddNode(root, ImGuiDockNodeFlags_DockSpace);
    ImGui::DockBuilderSetNodeSize(root, size);

    const float w = size.x > 1.0f ? size.x : 1440.0f;
    const float sc = theme::S();
    const float panelSc = 1.0f + (sc - 1.0f) * 0.55f;
    auto clampf = [](float v, float lo, float hi) { return v < lo ? lo : (v > hi ? hi : v); };
    const float minCenterPx = clampf(420.0f * panelSc, 320.0f, 0.52f * w);
    float leftPx  = clampf(360.0f * panelSc, 250.0f * panelSc, 0.34f * w);
    float rightPx = clampf(380.0f * panelSc, 270.0f * panelSc, 0.36f * w);
    const float sideMax = w - minCenterPx;
    if (leftPx + rightPx > sideMax && sideMax > 0.0f) {
        const float k = sideMax / (leftPx + rightPx);
        leftPx *= k;
        rightPx *= k;
    }
    const float leftFrac  = leftPx / w;
    const float rightFrac = rightPx / w;

    ImGuiID main = root, left, right, rightBottom;
    left = ImGui::DockBuilderSplitNode(main, ImGuiDir_Left, leftFrac, nullptr, &main);
    if (ImGuiDockNode* ln = ImGui::DockBuilderGetNode(left))
        ln->LocalFlags |= ImGuiDockNodeFlags_NoUndocking;

    right = ImGui::DockBuilderSplitNode(main, ImGuiDir_Right,
                                        rightFrac / (1.0f - leftFrac), nullptr, &main);
    rightBottom = ImGui::DockBuilderSplitNode(right, ImGuiDir_Down, 0.42f, nullptr, &right);

    // AI-CAD2 window names (see application.cpp Begin() titles).
    ImGui::DockBuilderDockWindow("Browser###left", left);
    ImGui::DockBuilderDockWindow("Assistant###agent", right);

    // Central node — 3D default-active; ECAD/CAM pre-registered for domain waves.
    ImGui::DockBuilderDockWindow("ECAD", main);
    ImGui::DockBuilderDockWindow("CAM", main);
    ImGui::DockBuilderDockWindow("Viewport###viewport", main);

    // Evaluate / assembly dialogs — land in lower-right on first open (host parity).
    static const char* kTaskPaneDialogs[] = {
        "Insert Component", "Mate##asm", "Exploded View",
        "Interference Detection", "Bill of Materials",
        "Mass Properties", "Measure", "Section Properties", "Geometry Check", "Symmetry Check",
        "Zebra Stripes", "Draft angle", "Wall thickness", "Mean curvature",
        "Configurations", "Design Table", "Cut List",
    };
    for (const char* d : kTaskPaneDialogs)
        ImGui::DockBuilderDockWindow(d, rightBottom);

    ImGui::DockBuilderFinish(root);
}

namespace ui {

namespace {

void drawDockSplittersRec(ImGuiDockNode* node, ImDrawList* dl, ImU32 col, float thickness) {
    if (!node || !node->IsVisible)
        return;
    if (!node->IsSplitNode())
        return;

    ImGuiDockNode* c0 = node->ChildNodes[0];
    ImGuiDockNode* c1 = node->ChildNodes[1];
    if (c0 && c1 && c0->IsVisible && c1->IsVisible) {
        const ImRect r0 = c0->Rect();
        const ImRect r1 = c1->Rect();
        if (node->SplitAxis == ImGuiAxis_X) {
            const float x = (r0.Max.x + r1.Min.x) * 0.5f;
            dl->AddLine(ImVec2(x, node->Pos.y),
                        ImVec2(x, node->Pos.y + node->Size.y), col, thickness);
        } else {
            const float y = (r0.Max.y + r1.Min.y) * 0.5f;
            dl->AddLine(ImVec2(node->Pos.x, y),
                        ImVec2(node->Pos.x + node->Size.x, y), col, thickness);
        }
    }

    drawDockSplittersRec(c0, dl, col, thickness);
    drawDockSplittersRec(c1, dl, col, thickness);
}

}  // namespace

void drawDockSplitters(ImGuiID dockRootId) {
    ImGuiDockNode* root = ImGui::DockBuilderGetNode(dockRootId);
    if (!root)
        return;

    const float S = theme::S();
    const ImU32 col = theme::roleU32A(theme::pal().line, 0.35f);
    const float thickness = std::max(1.f, 1.f * S);
    ImDrawList* dl = ImGui::GetWindowDrawList();
    drawDockSplittersRec(root, dl, col, thickness);
}

}  // namespace ui
