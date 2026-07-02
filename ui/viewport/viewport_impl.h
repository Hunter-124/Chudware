#pragma once
// OpenGL implementation of aicad::ui::IViewport. Wraps Renderer + IdPass; mesh/doc loading is
// driven by the worker layer via setMeshes / setDoc (not on the abstract interface).
#include <memory>
#include <set>
#include <string>
#include <vector>

#include "../abstract/viewport.h"
#include "renderer.h"
#include "render/aicm_loader.h"
#include "render/id_pass.h"

namespace aicad::viewport {

class GlViewport : public ui::IViewport {
public:
    GlViewport();
    ~GlViewport() override;

    bool init(int width, int height) override;
    void resize(int width, int height) override;
    void setSceneRevision(int revision) override;
    void render() override;
    std::uintptr_t colorTextureId() const override;
    ui::PickResult pick(const ui::PickRequest& req) override;
    ui::CameraState camera() const override;
    void setCamera(const ui::CameraState& cam) override;
    void orbit(float dx, float dy) override;
    void pan(float dx, float dy) override;
    void zoom(float delta) override;

    // F1 glass hooks (wired by F2 theme each frame).
    ViewportBackdrop backdrop() const;
    float averageLuminance(const ViewportRect& rect) const;

    // GPU pass timings + master switch for the blur/luminance pipeline (see Renderer).
    GlassTimings glassTimings() const;
    void setBackdropEnabled(bool on);
    bool backdropEnabled() const;

    Renderer& renderer() { return renderer_; }
    const Renderer& renderer() const { return renderer_; }
    render::IdPass& idPass() { return idPass_; }

    void setMeshes(const std::vector<Renderer::PartMesh>& parts);
    bool setDoc(const render::DocMesh& doc, std::string& err);
    void setHiddenBodies(const std::set<uint32_t>& bodyIds);

private:
    void syncPickPass();

    Renderer renderer_;
    render::IdPass idPass_;
    render::DocMesh doc_;
    bool hasDoc_ = false;

    float yaw_ = 35.f;
    float pitch_ = 28.f;
    float zoom_ = 1.f;
    float pan_[3] = {0.f, 0.f, 0.f};
    float target_[3] = {0.f, 0.f, 0.f};

    int sceneRevision_ = 0;
    int appliedRevision_ = -1;
    int width_ = 0;
    int height_ = 0;
};

}  // namespace aicad::viewport
