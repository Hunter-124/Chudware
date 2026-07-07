// Viewport panel: 3D canvas + HUD + tools (C4). GlViewport is the primary render path.
#pragma once

#include <string>
#include <array>
#include <map>
#include "imgui.h"
#include "theme/theme.h"

namespace app { struct AppState; }
namespace aicad::viewport { class GlViewport; }

namespace ui {

// Wire once at init (application.cpp). Replaces legacy Renderer* + IdPass* pair.
void setViewportPipeline(aicad::viewport::GlViewport* viewport);

// A6: enable/disable backdrop publishing + renderer blur/luminance work. Application::frame calls
// this with false when the Viewport###viewport window body does NOT execute this frame (hidden
// tab / no doc), and drawViewport re-enables it every frame it does draw.
void setViewportBackdropActive(bool active);

// Contrast halo color for icons::drawGlass on viewport/HUD surfaces: the vibrancy tone OPPOSITE
// surf's text tone, at ~0x50 alpha (A8 reserves literal white/black for theme.cpp/icons_sdf.cpp;
// this uses the theme-independent vibrancy tokens instead).
ImU32 glassIconHalo(const theme::AdaptiveSurface& surf, float alphaMul = 1.f);

// A4: mirror POD of aicad::viewport::GlassTimings (renderer.h), zeroed when no GlViewport is wired.
struct GlassTimings {
    float sceneMs = 0.f, blurMs = 0.f, lumaMs = 0.f;
    bool  valid = false;
};
GlassTimings viewportGlassTimings();

// Test/scenario harness: load an .aicm mesh straight into the GL viewport pipeline (the
// same path ensureDocLoaded uses), bypassing the engine. Returns false when the pipeline
// is not initialized or the file fails to parse.
bool viewportLoadAicm(const std::string& path);

// H4 (sim results): load a result-overlay .aicm (FEA/CFD worker output — render::DocMesh::scalars/
// displacements, ui/viewport/render/aicm_loader.h) straight into the GL viewport pipeline, exactly
// like viewportLoadAicm, but ALSO attaches the named per-vertex scalar field (empty = first field
// found on each body) as Renderer::PartMesh::scalar/scalarMin/scalarMax (and displacement, if the
// overlay carries one) so the shaded pass can colormap it. Turns colormap mode on. Returns true and
// fills vmin/vmax (raw SI units — the caller applies the display-unit factor) + dispScaleHint on
// success; false (state untouched) if the file/pipeline/field is missing.
bool viewportLoadResultAicm(const std::string& path, const std::string& scalarField,
                            float& vmin, float& vmax, float& dispScaleHint);

// Thin wrappers over the live GlViewport's Renderer so result panels (sim_manager) don't need the
// GL pipeline's internals — mirrors the C19 result colormap/deform controls host exposed via
// AppState::renderer directly.
void viewportSetColormapMode(bool on);
void viewportSetDeformScale(float scale);
float viewportDeformScale();
bool viewportScalarRange(float& vmin, float& vmax);

// Motion study: re-upload the current doc mesh with per-body model transforms applied in CPU.
// nameToMatrix maps component/body name -> 16-float column-major 4x4 model matrix.
// Calling with an empty map restores the identity-transform upload (i.e. clears motion pose).
// No-op when the viewport pipeline is not wired or the doc mesh was never loaded.
void viewportSetBodyTransforms(const std::map<std::string, std::array<float, 16>>& nameToMatrix);

// Draw the central Viewport dock window content (mesh, nav, HUD, tools).
void drawViewport(app::AppState& s);

// Camera + overlay state shared by camera_anim / mouse_gestures / view_selector / HUD.
struct ViewportState {
    float yaw   = 35.f;
    float pitch = 28.f;
    float zoom  = 1.f;
    float pan[3] = {0, 0, 0};

    bool  animActive = false;
    float animYaw0 = 0, animYaw1 = 0, animPitch0 = 0, animPitch1 = 0;
    float animZoom0 = 1, animZoom1 = 1;
    float animPan0[3] = {0, 0, 0}, animPan1[3] = {0, 0, 0};
    float animT = 0.f, animDur = 0.35f;

    int   displayStyle = 1;   // 0..4 (DisplayStyle)
    int   vpDragMode   = 0;   // 0=rotate, 1=pan
    bool  showPlane[3] = {true, true, true};
    bool  showTriad    = true;
    bool  showGroundShadow = true;
    bool  showRealView = false;

    bool  legendOn = false;
    float legendMin = 0.f, legendMax = 1.f;
    std::string legendTitle, legendUnits;

    bool  wantContextMenu = false;
};

ViewportState& viewportState();

// T-BL46: expose the file-scope SketchMode so scenarios.cpp can read elementDragActive().
// Returns a const ref — callers must not mutate it.
class SketchMode;
SketchMode& sketchMode();

// T-BL46: expose the GlViewport so scenarios.cpp can call renderer().camera() + worldToScreen()
// to compute exact screen coordinates for element hit tests.  Returns nullptr when not wired.
aicad::viewport::GlViewport* viewportGl();

// T-BL46: published viewport image rect (screen coords), updated each frame — safe to read from
// scenarios.cpp tick to get a/b for worldToScreen ViewportScreen and IsItemHovered coordinates.
ImVec2 viewportImageMin();
ImVec2 viewportImageMax();

}  // namespace ui
