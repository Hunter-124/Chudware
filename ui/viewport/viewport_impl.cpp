#include "viewport_impl.h"

#include <algorithm>
#include <cmath>

namespace aicad::viewport {

namespace {
constexpr float kPiOver180 = 0.017453292519943295f;
constexpr float kMinZoom = 0.1f;
constexpr float kMaxZoom = 10.f;
constexpr float kOrbitSens = 0.25f;
}  // namespace

GlViewport::GlViewport() = default;
GlViewport::~GlViewport() = default;

bool GlViewport::init(int width, int height) {
    width_ = width;
    height_ = height;
    std::string err;
    if (!renderer_.init(width, height, err)) return false;
    appliedRevision_ = -1;
    return true;
}

void GlViewport::resize(int width, int height) {
    width_ = width;
    height_ = height;
    renderer_.resize(width, height);
}

void GlViewport::setSceneRevision(int revision) {
    sceneRevision_ = revision;
}

void GlViewport::setMeshes(const std::vector<Renderer::PartMesh>& parts) {
    renderer_.setMeshes(parts);
}

bool GlViewport::setDoc(const render::DocMesh& doc, std::string& err) {
    doc_ = doc;
    hasDoc_ = true;
    if (!idPass_.setDoc(doc, err)) return false;
    return true;
}

void GlViewport::setHiddenBodies(const std::set<uint32_t>& bodyIds) {
    idPass_.setHiddenBodies(bodyIds);
}

void GlViewport::syncPickPass() {
    if (!hasDoc_) return;
    float normals[3][3] = {};
    float offsets[3] = {};
    int clipCount = renderer_.sectionClip(normals, offsets);
    idPass_.setSectionClip(clipCount, normals, offsets);
    idPass_.render(renderer_.camera());
}

void GlViewport::render() {
    if (sceneRevision_ != appliedRevision_) {
        appliedRevision_ = sceneRevision_;
    }
    renderer_.renderOrbit(yaw_, pitch_, zoom_, pan_);
    syncPickPass();
}

std::uintptr_t GlViewport::colorTextureId() const {
    return static_cast<std::uintptr_t>(renderer_.resolvedColorTexture());
}

ViewportBackdrop GlViewport::backdrop() const {
    return renderer_.backdrop();
}

float GlViewport::averageLuminance(const ViewportRect& rect) const {
    return renderer_.averageLuminance(rect);
}

ui::PickResult GlViewport::pick(const ui::PickRequest& req) {
    ui::PickResult out;
    if (!hasDoc_ || width_ <= 0 || height_ <= 0) return out;

    syncPickPass();
    render::PickHit hit = idPass_.pickFinalPreferEdge(req.x, req.y, req.width > 0 ? req.width : width_,
                                                      req.height > 0 ? req.height : height_, 4);
    if (!hit.valid()) return out;
    out.hit = true;
    out.bodyId = static_cast<int>(hit.body_id);
    out.topoId = static_cast<int>(hit.topo_id);
    switch (hit.kind) {
        case render::PickKind::Face: out.kind = "face"; break;
        case render::PickKind::Edge: out.kind = "edge"; break;
        case render::PickKind::Vertex: out.kind = "vertex"; break;
        default: out.kind = ""; break;
    }
    return out;
}

ui::CameraState GlViewport::camera() const {
    ui::CameraState c;
    c.yaw = yaw_;
    c.pitch = pitch_;
    c.distance = zoom_;
    c.target[0] = target_[0];
    c.target[1] = target_[1];
    c.target[2] = target_[2];
    return c;
}

void GlViewport::setCamera(const ui::CameraState& cam) {
    yaw_ = cam.yaw;
    pitch_ = cam.pitch;
    zoom_ = std::clamp(cam.distance, kMinZoom, kMaxZoom);
    target_[0] = cam.target[0];
    target_[1] = cam.target[1];
    target_[2] = cam.target[2];
    pan_[0] = cam.target[0];
    pan_[1] = cam.target[1];
    pan_[2] = cam.target[2];
}

void GlViewport::orbit(float dx, float dy) {
    yaw_ += dx * kOrbitSens;
    pitch_ += dy * kOrbitSens;
    if (pitch_ > 89.5f) pitch_ = 89.5f;
    if (pitch_ < -89.5f) pitch_ = -89.5f;
}

void GlViewport::pan(float dx, float dy) {
    const float sceneR = renderer_.effectiveSceneRadius();
    const float zc = zoom_ < 0.05f ? 0.05f : zoom_;
    const float dist = sceneR * 2.2f / zc;
    const float fovRad = Renderer::kFovDeg * kPiOver180;
    const float worldPerPx = 2.f * dist * std::tan(fovRad * 0.5f) /
                             std::max(1, height_);

    float yr = yaw_ * kPiOver180;
    float pr = pitch_ * kPiOver180;
    Vec3 F{std::cos(pr) * std::sin(yr), -std::cos(pr) * std::cos(yr), std::sin(pr)};
    Vec3 R = normalize(cross({0, 0, 1}, F));
    Vec3 U = cross(F, R);

    pan_[0] += (-R.x * dx + U.x * dy) * worldPerPx;
    pan_[1] += (-R.y * dx + U.y * dy) * worldPerPx;
    pan_[2] += (-R.z * dx + U.z * dy) * worldPerPx;
    target_[0] = pan_[0];
    target_[1] = pan_[1];
    target_[2] = pan_[2];
}

void GlViewport::zoom(float delta) {
    const float factor = std::exp(delta * 0.1f);
    zoom_ = std::clamp(zoom_ * factor, kMinZoom, kMaxZoom);
}

}  // namespace aicad::viewport
