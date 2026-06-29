#pragma once
#include <cstdint>
#include <functional>
#include <optional>
#include <string>

namespace aicad::ui {

struct ViewportSize {
    int width = 0;
    int height = 0;
};

struct PickRequest {
    int x = 0;
    int y = 0;
    int width = 0;
    int height = 0;
};

struct PickResult {
    bool hit = false;
    int bodyId = 0;
    std::string kind;
    int topoId = 0;
};

struct CameraState {
    float distance = 5.f;
    float yaw = 0.f;
    float pitch = 0.3f;
    float target[3] = {0.f, 0.f, 0.f};
};

// Framework-neutral viewport interface. Implemented by ui/viewport/ (GL pipeline).
class IViewport {
public:
    virtual ~IViewport() = default;
    virtual bool init(int width, int height) = 0;
    virtual void resize(int width, int height) = 0;
    virtual void setSceneRevision(int revision) = 0;
    virtual void render() = 0;
    virtual std::uintptr_t colorTextureId() const = 0;  // backend-specific handle
    virtual PickResult pick(const PickRequest& req) = 0;
    virtual CameraState camera() const = 0;
    virtual void setCamera(const CameraState& cam) = 0;
    virtual void orbit(float dx, float dy) = 0;
    virtual void pan(float dx, float dy) = 0;
    virtual void zoom(float delta) = 0;
};

} // namespace aicad::ui
