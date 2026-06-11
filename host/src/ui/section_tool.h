#pragma once
// AI-CAD Section View — SolidWorks-style planar section (C25, wave W3-I).
// A single clip plane (Front/Top/Right presets, offset, flip) cuts the model; the viewport shows
// a translucent blue plane quad with an ORANGE offset-drag arrow. A compact PM-style panel docks
// to the viewport top-left. OK collapses to a "Section ✓" chip (clipping stays on); X/Esc disable.
// The renderer owns the actual clip + cap (Renderer::setSectionPlane); this class is pure ImGui +
// per-frame state and pushes the plane to the renderer each frame.
#include "imgui.h"

struct AppState;
class Renderer;

namespace ui {

class SectionTool {
public:
    // Contract with the HUD/context-menu agent: a file-local `static SectionTool s_sectionTool;`
    // in viewport_panel.cpp; the HUD Section button + context item call these.
    void toggle(AppState& st);          // activate with defaults / deactivate (disables clipping)
    bool active() const { return m_active; }

    // Call every frame in the viewport overlay tail (after the view-selector calls). Pushes the
    // clip plane to the renderer, draws the PM panel + the in-viewport plane/arrow manipulator,
    // and handles Esc + the offset drag. While sketching the clip is auto-suspended and restored.
    void updateAndDraw(AppState& st, Renderer& renderer, bool imgHovered, bool inSketch,
                       ImVec2 vr0, ImVec2 vr1);

private:
    enum class Preset { Front, Top, Right };
    void applyPreset(Renderer& renderer, Preset p);   // sets normal + recenters offset on the model
    void pushPlane(Renderer& renderer);               // sync state -> renderer.setSectionPlane

    bool   m_active = false;       // tool engaged (clip applied)
    bool   m_collapsed = false;    // OK pressed → panel becomes a small chip
    bool   m_showCap = true;
    bool   m_flip = false;         // negates the chosen preset normal
    Preset m_preset = Preset::Right;
    float  m_normal[3] = {1, 0, 0};
    float  m_offset = 0.0f;        // along m_normal, model units
    bool   m_haveModel = false;    // set once a model is present (offset initialized to center)
    bool   m_envChecked = false;   // AI_CAD_SECTION auto-activate (one-shot)
    bool   m_dragging = false;       // arrow drag in progress
    float  m_dragStartOffset = 0;    // offset at drag start
    ImVec2 m_dragStartMouse{0, 0};   // mouse pos at drag start (screen px)
    float  m_dragWorldPerPx = 0;     // world units along the normal per screen px along the arrow
    float  m_dragDirX = 0, m_dragDirY = 0;  // unit screen-direction of +normal at drag start
};

}  // namespace ui
