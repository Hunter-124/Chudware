// AI-ECAD UI scaffold (Wave D) — NOT yet build-verified; see docs/ECAD_UI_INTEGRATION.md
#include "imgui.h"

#include "ecad_panel.h"

#include <cstdio>

#include "../../core/app_state.h"
#include "../../ui_theme.h"

#include "ecad_canvas.h"
#include "ecad_mirror.h"

namespace ui {

namespace {

// Scaffold-local editor state. In the permanent wiring these three move onto AppState (see
// ECAD_UI_INTEGRATION.md §(e)); kept here so the scaffold honours the "do not edit AppState" rule.
struct EcadEditorState {
    EcadMirror mirror;
    EcadCanvas canvas;
    EcadMirror::Kind lastKind = EcadMirror::Kind::None;
    float leftW = 200.0f;     // left sub-panel width (px, pre-scale)
    bool  showLeft = true;
};

EcadEditorState& state() {
    static EcadEditorState s;
    return s;
}

ImVec4 v4(const float c[4], float a = 1.0f) { return ImVec4(c[0], c[1], c[2], c[3] * a); }

// Copper-layer swatch color, kept in sync with ecad_canvas.cpp::copperColor (palette roles, theme
// aware). Returns an ImVec4 for ImGui widget tinting.
ImVec4 layerSwatch(int copperIndex) {
    const ui::Palette& p = ui::palette;
    switch (((copperIndex % 6) + 6) % 6) {
        case 0:  return v4(p.axisX);
        case 1:  return v4(p.axisZ);
        case 2:  return v4(p.statusOk);
        case 3:  return v4(p.statusWarn);
        case 4:  return v4(p.axisY);
        default: return v4(p.statusErr);
    }
}

// ---- left sub-panel: PCB layers / sch nets ----
void drawLeftPanel(EcadEditorState& es) {
    const ui::Palette& pal = ui::palette;
    EcadMirror& m = es.mirror;

    if (m.isPcb()) {
        ImGui::TextColored(v4(pal.textDim), "Layers");
        ImGui::Separator();
        for (const auto& ly : m.layers()) {
            ImGui::PushID(ly.id.c_str());
            bool vis = m.layerVisible(ly.id);
            if (ImGui::Checkbox("##v", &vis)) m.setLayerVisible(ly.id, vis);
            ImGui::SameLine();
            // color swatch
            const ImVec4 sw = layerSwatch(ly.copperIndex);
            ImVec2 p0 = ImGui::GetCursorScreenPos();
            const float s = ImGui::GetTextLineHeight();
            ImGui::GetWindowDrawList()->AddRectFilled(
                p0, ImVec2(p0.x + s, p0.y + s), ui::Palette::toU32(&sw.x), 2.0f);
            ImGui::Dummy(ImVec2(s + 6, s));
            ImGui::SameLine();
            ImGui::TextColored(vis ? v4(pal.text) : v4(pal.textDisabled), "%s", ly.id.c_str());
            ImGui::PopID();
        }
        ImGui::Spacing();
        ImGui::TextColored(v4(pal.textDim), "Nets (%d)", (int)m.nets().size());
        ImGui::Separator();
        for (const auto& n : m.nets())
            ImGui::TextColored(v4(pal.text), "%s", n.name.c_str());
    } else if (m.isSch()) {
        ImGui::TextColored(v4(pal.textDim), "Nets (%d)", (int)m.nets().size());
        ImGui::Separator();
        for (const auto& n : m.nets()) {
            ImGui::TextColored(v4(pal.text), "%s", n.name.c_str());
            ImGui::SameLine();
            ImGui::TextColored(v4(pal.textDim), "(%d pins)", n.pinCount);
        }
        ImGui::Spacing();
        ImGui::TextColored(v4(pal.textDim), "Symbols (%d)", (int)m.symbols().size());
        ImGui::Separator();
        for (const auto& s : m.symbols()) {
            ImGui::TextColored(v4(pal.text), "%s", s.ref.empty() ? s.libid.c_str() : s.ref.c_str());
            if (!s.value.empty()) {
                ImGui::SameLine();
                ImGui::TextColored(v4(pal.textDim), "%s", s.value.c_str());
            }
        }
    }
}

// ---- bottom strip: ERC / DRC results ----
void drawResultsStrip(AppState& st, EcadEditorState& es) {
    const ui::Palette& pal = ui::palette;
    EcadMirror& m = es.mirror;

    const auto& list = m.isSch() ? m.erc() : m.drc();
    int errs = 0, warns = 0;
    for (const auto& v : list) (v.severity == "error" ? errs : warns)++;

    const char* kindLbl = m.isSch() ? "ERC" : "DRC";
    ImGui::TextColored(errs ? v4(pal.statusErr) : v4(pal.statusOk),
                       "%s: %d error%s", kindLbl, errs, errs == 1 ? "" : "s");
    ImGui::SameLine();
    ImGui::TextColored(warns ? v4(pal.statusWarn) : v4(pal.textDim),
                       "  %d warning%s", warns, warns == 1 ? "" : "s");
    if (m.isPcb()) {
        ImGui::SameLine();
        const int un = m.unroutedCount();
        ImGui::TextColored(un ? v4(pal.statusWarn) : v4(pal.statusOk),
                           "  |  Unrouted: %d", un);
    }
    ImGui::Separator();

    ImGui::BeginChild("##ecadResults", ImVec2(0, 0), ImGuiChildFlags_None);
    for (size_t i = 0; i < list.size(); ++i) {
        const EcadViolation& v = list[i];
        const ImVec4 c = (v.severity == "error") ? v4(pal.statusErr) : v4(pal.statusWarn);
        // One full-width selectable carrying the whole row; tint the text by the severity. Clicking
        // logs (the scaffold). The permanent hook is a viewport "zoom to violation" using v.loc
        // (guide §(e)). A unique ##id keeps ImGui ids stable across frames.
        char label[256];
        std::snprintf(label, sizeof(label), "[%s] %s%s%s##viol%zu",
                      v.severity.c_str(), v.rule.c_str(),
                      v.message.empty() ? "" : " — ", v.message.c_str(), i);
        ImGui::PushStyleColor(ImGuiCol_Text, c);
        const bool clicked = ImGui::Selectable(label);
        ImGui::PopStyleColor();
        if (clicked) {
            if (v.hasLoc)
                st.addLog("[..] ECAD " + v.rule + " @ (" +
                          std::to_string(v.loc.x) + ", " + std::to_string(v.loc.y) + ")");
            else
                st.addLog("[..] ECAD " + v.rule);
        }
    }
    if (list.empty())
        ImGui::TextColored(v4(pal.textDim), "No %s violations.", kindLbl);
    ImGui::EndChild();
}

}  // namespace

// ---- public API ----

EcadMirror& ecadEditorMirror() { return state().mirror; }

void ecadEditorLoadResponse(const nlohmann::json& resp) {
    EcadEditorState& es = state();
    es.mirror.loadFromTree(resp);
    if (es.mirror.kind() != es.lastKind) {
        es.canvas.requestFit();
        es.lastKind = es.mirror.kind();
    }
}

void drawEcadEditor(AppState& st) {
    EcadEditorState& es = state();
    const ui::Palette& pal = ui::palette;

    // Pop the ECAD tab forward the frame a document first appears (New Schematic/PCB, or a shot),
    // so the editor surfaces instead of staying hidden behind the Viewport tab.
    static bool prevHadDoc = false;
    const bool hasDocNow = es.mirror.hasDoc();
    if (hasDocNow && !prevHadDoc) ImGui::SetWindowFocus("ECAD");
    prevHadDoc = hasDocNow;

    if (!ImGui::Begin("ECAD")) { ImGui::End(); return; }

    if (!es.mirror.hasDoc()) {
        ImGui::TextColored(v4(pal.textDim),
                           "No ECAD document open. Use the PCB ribbon (New Schematic / New PCB).");
        ImGui::End();
        return;
    }

    // ---- header toolbar: doc badge + name + revision (left); view controls (right) ----
    const float sc = ui::uiScale;
    ImGui::AlignTextToFramePadding();
    ImGui::TextColored(v4(pal.accent), "%s", es.mirror.isSch() ? "SCHEMATIC" : "PCB LAYOUT");
    ImGui::SameLine(0, 12 * sc);
    ImGui::TextColored(v4(pal.text), "%s",
                       es.mirror.name().empty() ? "(untitled)" : es.mirror.name().c_str());
    ImGui::SameLine(0, 8 * sc);
    ImGui::TextColored(v4(pal.textDim), "rev %d", es.mirror.revision());
    ImGui::SameLine(ImGui::GetWindowContentRegionMax().x - 160 * sc);
    if (ImGui::SmallButton("Zoom Fit")) es.canvas.requestFit();
    ImGui::SameLine();
    ImGui::Checkbox(es.mirror.isPcb() ? "Layers" : "Nets", &es.showLeft);

    ImGui::Separator();

    // Reserve the bottom results strip; the rest splits left sub-panel | canvas.
    const float bottomH = 140.0f * sc;
    const ImVec2 avail = ImGui::GetContentRegionAvail();
    const float mainH = avail.y - bottomH;

    // Left sub-panel.
    if (es.showLeft) {
        ImGui::BeginChild("##ecadLeft", ImVec2(es.leftW * sc, mainH), ImGuiChildFlags_Border);
        drawLeftPanel(es);
        ImGui::EndChild();
        ImGui::SameLine();
    }

    // Canvas.
    ImGui::BeginChild("##ecadCanvasHost",
                      ImVec2(0, mainH), ImGuiChildFlags_Border,
                      ImGuiWindowFlags_NoScrollbar | ImGuiWindowFlags_NoScrollWithMouse);
    const ImVec2 canvasAvail = ImGui::GetContentRegionAvail();
    EcadCanvasIO io = es.canvas.draw(es.mirror, canvasAvail);
    // Scaffold: surface the world cursor in the corner (the worker pick hook is documented, not wired).
    if (io.hasWorldMouse) {
        char buf[64];
        std::snprintf(buf, sizeof(buf), "%.2f, %.2f mm", io.worldMouse.x, io.worldMouse.y);
        ImVec2 p = ImGui::GetWindowPos();
        ImGui::GetWindowDrawList()->AddText(
            ImVec2(p.x + 8, p.y + ImGui::GetWindowHeight() - 20),
            ui::Palette::toU32(pal.textDim), buf);
    }
    ImGui::EndChild();

    // Bottom results strip.
    ImGui::BeginChild("##ecadStrip", ImVec2(0, 0), ImGuiChildFlags_Border);
    drawResultsStrip(st, es);
    ImGui::EndChild();

    ImGui::End();
}

}  // namespace ui
