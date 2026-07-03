// T-U7 shared network-canvas renderer — piping / hydraulic / structural viewers.
// Renders net_canvas_view.h view-model with ImDrawList (no image assets).
// ISO 1219 hydraulic symbols: inline geometric primitives.
#include "net_canvas_panel.h"
#include "net_canvas_view.h"
#include "theme/theme.h"
#include "theme/tokens.h"

#include "imgui.h"
#include "imgui_internal.h"

#include <algorithm>
#include <cmath>
#include <cstring>
#include <string>

namespace ui {

// ── ISO 1219 symbol helpers (all in screen space) ─────────────────────────────

static void drawIsoSymbol(ImDrawList* dl, const std::string& compType,
                          ImVec2 center, float halfW, float halfH, ImU32 col) {
    if (compType == "pump" || compType == "motor_hydraulic") {
        // Circle with arrow inside
        dl->AddCircle(center, halfW * 0.9f, col, 20, 1.5f);
        // Arrow pointing right for pump
        float ar = halfW * 0.4f;
        dl->AddLine({center.x - ar, center.y}, {center.x + ar, center.y}, col, 1.5f);
        dl->AddLine({center.x + ar * 0.3f, center.y - ar * 0.4f},
                    {center.x + ar, center.y}, col, 1.5f);
        dl->AddLine({center.x + ar * 0.3f, center.y + ar * 0.4f},
                    {center.x + ar, center.y}, col, 1.5f);
    } else if (compType == "cylinder_linear") {
        // Rectangle with piston line
        float rw = halfW * 0.85f, rh = halfH * 0.55f;
        dl->AddRect({center.x - rw, center.y - rh},
                    {center.x + rw, center.y + rh}, col, 0.f, 0, 1.5f);
        // Piston rod extending right
        dl->AddLine({center.x + rw * 0.0f, center.y},
                    {center.x + rw * 1.3f, center.y}, col, 2.f);
        // Piston line
        dl->AddLine({center.x, center.y - rh},
                    {center.x, center.y + rh}, col, 2.f);
    } else if (compType == "valve_directional" || compType == "valve_check"
               || compType == "valve_relief" || compType == "valve_flow_control") {
        // Diamond (ISO valve symbol base)
        float d = halfW * 0.75f;
        ImVec2 pts[4] = {
            {center.x,     center.y - d},
            {center.x + d, center.y    },
            {center.x,     center.y + d},
            {center.x - d, center.y    },
        };
        dl->AddPolyline(pts, 4, col, ImDrawFlags_Closed, 1.5f);
        if (compType == "valve_check") {
            // Check valve arrow
            dl->AddLine({center.x - d * 0.4f, center.y},
                        {center.x + d * 0.4f, center.y}, col, 1.5f);
        }
    } else if (compType == "accumulator") {
        // Circle with horizontal line (ISO accumulator)
        dl->AddCircle(center, halfW * 0.75f, col, 20, 1.5f);
        dl->AddLine({center.x - halfW * 0.75f, center.y},
                    {center.x + halfW * 0.75f, center.y}, col, 1.5f);
    } else if (compType == "reservoir") {
        // Three horizontal lines (open tank symbol)
        float w2 = halfW * 0.7f;
        for (int i = -1; i <= 1; ++i) {
            float oy = center.y + i * halfH * 0.35f;
            dl->AddLine({center.x - w2, oy}, {center.x + w2, oy}, col, 1.5f);
        }
    } else {
        // Generic: plain inner rect
        dl->AddRect({center.x - halfW * 0.7f, center.y - halfH * 0.5f},
                    {center.x + halfW * 0.7f, center.y + halfH * 0.5f}, col, 0.f, 0, 1.5f);
    }
}

// ── Canvas coordinate transforms ──────────────────────────────────────────────

static ImVec2 modelToScreen(float mx, float my,
                             ImVec2 canvasOrigin, float panX, float panY, float zoom) {
    return { canvasOrigin.x + (mx + panX) * zoom,
             canvasOrigin.y + (my + panY) * zoom };
}

// ── Main draw function ─────────────────────────────────────────────────────────

void drawNetCanvas(aicad::ui::NetCanvasModel& model) {
    using namespace aicad::ui;

    if (model.state == NetCanvasPanelState::Idle) {
        ImGui::TextDisabled("No document loaded.");
        return;
    }
    if (model.state == NetCanvasPanelState::Loading) {
        ImGui::TextDisabled("Loading\xe2\x80\xa6");
        return;
    }
    if (model.state == NetCanvasPanelState::Error) {
        ImGui::PushStyleColor(ImGuiCol_Text, TCOL(bad));
        ImGui::TextWrapped("Error: %s", model.errorMsg.c_str());
        ImGui::PopStyleColor();
        return;
    }

    // Edit affordance (v1 read-only)
    ImGui::SameLine(ImGui::GetContentRegionAvail().x - 130.f);
    if (ImGui::SmallButton("Open Copilot \xe2\x9c\x8f")) {}
    if (ImGui::IsItemHovered())
        ImGui::SetTooltip("%s", NET_CANVAS_EDIT_HINT);
    ImGui::Separator();

    // Solve status strip
    if (model.solved) {
        ImU32 stripCol = model.converged ? TCOL(good) : TCOL(warn);
        ImGui::PushStyleColor(ImGuiCol_Text, stripCol);
        ImGui::TextUnformatted(model.solveStatus.c_str());
        ImGui::PopStyleColor();
        ImGui::Separator();
    }

    // Two-column layout: canvas | results
    const float totalW = ImGui::GetContentRegionAvail().x;
    const float canvasW = totalW * 0.65f;
    const float resultsW = totalW - canvasW - ImGui::GetStyle().ItemSpacing.x;
    const float availH = ImGui::GetContentRegionAvail().y - 4.f;

    // ── Canvas column ─────────────────────────────────────────────────────────
    ImGui::BeginChild("##nc_canvas", ImVec2(canvasW, availH), false,
                      ImGuiWindowFlags_NoScrollbar | ImGuiWindowFlags_NoScrollWithMouse);

    ImDrawList* dl = ImGui::GetWindowDrawList();
    ImVec2 canvasOrigin = ImGui::GetCursorScreenPos();
    ImVec2 canvasSize   = ImGui::GetContentRegionAvail();

    const float nodeW = 90.f, nodeH = 48.f;

    // Auto-fit content on first draw (and after reseed): center the node bbox
    // in the canvas at a zoom that leaves a margin.
    if (!model.viewFitted && !model.nodes.empty()
        && canvasSize.x > 50.f && canvasSize.y > 50.f) {
        float minX = model.nodes[0].x, maxX = minX;
        float minY = model.nodes[0].y, maxY = minY;
        for (const auto& n : model.nodes) {
            minX = std::min(minX, n.x); maxX = std::max(maxX, n.x);
            minY = std::min(minY, n.y); maxY = std::max(maxY, n.y);
        }
        const float margin = 60.f;
        float spanX = (maxX - minX) + nodeW + margin * 2.f;
        float spanY = (maxY - minY) + nodeH + margin * 2.f;
        float fitZoom = std::min(canvasSize.x / spanX, canvasSize.y / spanY);
        model.zoom = std::max(0.1f, std::min(2.f, fitZoom));
        // Center of bbox → center of canvas
        float cx = (minX + maxX) * 0.5f;
        float cy = (minY + maxY) * 0.5f;
        model.panX = (canvasSize.x * 0.5f) / model.zoom - cx;
        model.panY = (canvasSize.y * 0.5f) / model.zoom - cy;
        model.viewFitted = true;
    }

    // Background
    dl->AddRectFilled(canvasOrigin,
                      {canvasOrigin.x + canvasSize.x, canvasOrigin.y + canvasSize.y},
                      TCOL(bg0));

    // Mouse interactions (pan + zoom) — invisible button captures events
    ImGui::InvisibleButton("##nc_bg", canvasSize,
        ImGuiButtonFlags_MouseButtonLeft | ImGuiButtonFlags_MouseButtonRight);
    bool bgHovered = ImGui::IsItemHovered();
    bool bgActive  = ImGui::IsItemActive();

    if (bgHovered) {
        float wheel = ImGui::GetIO().MouseWheel;
        if (wheel != 0.f) {
            // Zoom toward cursor
            ImVec2 mousePos = ImGui::GetIO().MousePos;
            float  mx = (mousePos.x - canvasOrigin.x) / model.zoom - model.panX;
            float  my = (mousePos.y - canvasOrigin.y) / model.zoom - model.panY;
            model.zoom = std::max(0.1f, std::min(8.f, model.zoom * (1.f + wheel * 0.1f)));
            model.panX = (mousePos.x - canvasOrigin.x) / model.zoom - mx;
            model.panY = (mousePos.y - canvasOrigin.y) / model.zoom - my;
        }
    }
    if (bgActive && ImGui::IsMouseDragging(ImGuiMouseButton_Left)) {
        ImVec2 delta = ImGui::GetIO().MouseDelta;
        model.panX += delta.x / model.zoom;
        model.panY += delta.y / model.zoom;
    }

    // Reset view button
    ImGui::SetCursorScreenPos({canvasOrigin.x + 4.f, canvasOrigin.y + 4.f});
    if (ImGui::SmallButton("\xe2\x9f\xb3")) { model.viewFitted = false; }

    // Draw edges
    for (const auto& edge : model.edges) {
        const NetCanvasNode* nA = nullptr;
        const NetCanvasNode* nB = nullptr;
        for (const auto& n : model.nodes) {
            if (n.id == edge.fromId) nA = &n;
            if (n.id == edge.toId)   nB = &n;
        }
        if (!nA || !nB) continue;
        ImVec2 pA = modelToScreen(nA->x, nA->y, canvasOrigin, model.panX, model.panY, model.zoom);
        ImVec2 pB = modelToScreen(nB->x, nB->y, canvasOrigin, model.panX, model.panY, model.zoom);
        ImU32 lineCol = theme::roleU32A(theme::pal().line, 200 / 255.f);
        if (edge.dashed) {
            // Simple dashed: draw segments
            float dx = pB.x - pA.x, dy = pB.y - pA.y;
            float len = std::sqrt(dx*dx + dy*dy);
            if (len > 0.f) {
                float segLen = 8.f, gapLen = 5.f, step = segLen + gapLen;
                float nx = dx / len, ny = dy / len;
                float t = 0.f;
                while (t < len) {
                    float t2 = std::min(t + segLen, len);
                    dl->AddLine({pA.x + nx*t, pA.y + ny*t},
                                {pA.x + nx*t2, pA.y + ny*t2}, lineCol, 1.5f);
                    t += step;
                }
            }
        } else {
            dl->AddLine(pA, pB, lineCol, 1.5f);
        }
        // Edge label midpoint
        if (!edge.label.empty()) {
            float efs = std::max(8.f, ImGui::GetFontSize() * model.zoom * 0.85f);
            ImVec2 mid = {(pA.x + pB.x) * 0.5f, (pA.y + pB.y) * 0.5f - efs * 0.6f};
            dl->AddText(ImGui::GetFont(), efs, mid,
                        theme::roleU32A(theme::pal().txt1, 220 / 255.f), edge.label.c_str());
        }
    }

    // Draw nodes
    int clickedIdx = -1;
    for (int i = 0; i < (int)model.nodes.size(); ++i) {
        const auto& node = model.nodes[i];
        ImVec2 center = modelToScreen(node.x, node.y, canvasOrigin,
                                      model.panX, model.panY, model.zoom);
        float hw = nodeW * model.zoom * 0.5f;
        float hh = nodeH * model.zoom * 0.5f;
        ImVec2 tl = {center.x - hw, center.y - hh};
        ImVec2 br = {center.x + hw, center.y + hh};

        bool selected = (model.selectedNode == i);
        ImU32 fillCol = selected ? theme::roleU32A(theme::pal().active, 220 / 255.f)
                                 : theme::roleU32A(theme::pal().bg3, 220 / 255.f);
        ImU32 borderCol = selected ? TCOL(accent) : TCOL(glassBorder);
        if (!node.resultOk && !node.resultStr.empty())
            borderCol = TCOL(bad);

        dl->AddRectFilled(tl, br, fillCol, 6.f * model.zoom);
        dl->AddRect(tl, br, borderCol, 6.f * model.zoom, 0, 1.5f);

        // ISO symbol (hydraulic)
        drawIsoSymbol(dl, node.sublabel, center, hw * 0.6f, hh * 0.6f,
                      theme::roleU32A(theme::pal().txt2, 180 / 255.f));

        // Label text (scaled with zoom so it stays inside the node box)
        float fs = std::max(7.f, ImGui::GetFontSize() * model.zoom * 0.8f);
        float lineH = fs + 1.f;
        ImVec2 textPos = {tl.x + 4.f * model.zoom, tl.y + 3.f * model.zoom};
        ImFont* font = ImGui::GetFont();
        dl->AddText(font, fs, textPos, TCOL(txt0), node.label.c_str());
        if (!node.sublabel.empty()) {
            dl->AddText(font, fs, {textPos.x, textPos.y + lineH},
                        theme::roleU32A(theme::pal().txt2, 200 / 255.f), node.sublabel.c_str());
        }
        if (!node.resultStr.empty()) {
            ImU32 resCol = node.resultOk ? theme::roleU32A(theme::pal().good, 230 / 255.f)
                                         : theme::roleU32A(theme::pal().bad, 230 / 255.f);
            dl->AddText(font, fs, {textPos.x, textPos.y + lineH * 2.f},
                        resCol, node.resultStr.c_str());
        }

        // Click detection (InvisibleButton over each node)
        ImGui::SetCursorScreenPos(tl);
        ImGui::PushID(i);
        if (ImGui::InvisibleButton("##node", {hw * 2.f, hh * 2.f}))
            clickedIdx = i;
        ImGui::PopID();
    }

    if (clickedIdx >= 0)
        model.selectedNode = clickedIdx;

    ImGui::EndChild();

    // ── Results column ────────────────────────────────────────────────────────
    ImGui::SameLine();
    ImGui::BeginChild("##nc_results", ImVec2(resultsW, availH), false);
    ImGui::TextDisabled("Results");
    ImGui::Separator();

    if (model.results.empty()) {
        ImGui::TextDisabled("No solve results available.");
        ImGui::Spacing();
        ImGui::TextDisabled("Run a solve via the Copilot,");
        ImGui::TextDisabled("then reopen this viewer.");
    } else {
        for (int i = 0; i < (int)model.results.size(); ++i) {
            const auto& r = model.results[i];
            bool highlight = (model.selectedNode >= 0 &&
                              i < (int)model.nodes.size() &&
                              model.nodes[i].id == r.elementId);
            if (highlight) ImGui::PushStyleColor(ImGuiCol_Text, TCOL(accent));
            ImGui::TextWrapped("%s", r.label.c_str());
            if (highlight) ImGui::PopStyleColor();

            ImU32 valCol = r.ok ? TCOL(good) : TCOL(bad);
            ImGui::PushStyleColor(ImGuiCol_Text, valCol);
            ImGui::TextWrapped("  %s", r.value.c_str());
            ImGui::PopStyleColor();
            ImGui::Spacing();
        }
    }

    // Selected node detail
    if (model.selectedNode >= 0 && model.selectedNode < (int)model.nodes.size()) {
        ImGui::Separator();
        ImGui::TextDisabled("Selected:");
        const auto& n = model.nodes[model.selectedNode];
        ImGui::TextWrapped("%s", n.label.c_str());
        if (!n.sublabel.empty()) ImGui::TextWrapped("%s", n.sublabel.c_str());
        if (!n.resultStr.empty()) {
            ImGui::PushStyleColor(ImGuiCol_Text, n.resultOk ? TCOL(good)
                                                             : TCOL(bad));
            ImGui::TextWrapped("%s", n.resultStr.c_str());
            ImGui::PopStyleColor();
        }
    }

    ImGui::EndChild();
}

}  // namespace ui
