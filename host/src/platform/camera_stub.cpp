#include "camera.h"
#include <memory>
#include <cstdlib>
#include <cstring>

namespace aicad::platform {

class StubCameraBackend : public CameraBackend {
    bool opened_ = false;
public:
    std::vector<CameraInfo> listDevices() override {
        return {{"stub:0", "Stub Camera"}};
    }
    bool open(const std::string& id) override {
        if (id.empty() || id == "stub:0") { opened_ = true; return true; }
        return false;
    }
    void close() override { opened_ = false; }
    bool lockExposure(double, double) override { return true; }
    bool grabFrame(CameraFrame& out) override {
        if (!opened_) return false;
        out.width = 320; out.height = 240;
        out.rgba.resize(320 * 240 * 4);
        for (int y = 0; y < 240; ++y)
            for (int x = 0; x < 320; ++x) {
                int i = (y * 320 + x) * 4;
                out.rgba[i+0] = (uint8_t)(x % 256);
                out.rgba[i+1] = (uint8_t)(y % 256);
                out.rgba[i+2] = 128;
                out.rgba[i+3] = 255;
            }
        out.timestamp_sec = 1.0;
        return true;
    }
};

#if defined(__linux__) && !defined(_WIN32)
// Forward declaration — implemented in camera_v4l2.cpp
std::unique_ptr<CameraBackend> createV4L2Camera();
#endif

#ifdef _WIN32
std::unique_ptr<CameraBackend> createMFCamera();
#endif

std::unique_ptr<CameraBackend> CameraBackend::create() {
#if defined(__linux__) && !defined(_WIN32)
    const char* ev = std::getenv("AICAD_TEST_CAMERA");
    if (ev && std::strcmp(ev, "1") == 0)
        return createV4L2Camera();
#elif defined(_WIN32)
    return createMFCamera();
#endif
    return std::make_unique<StubCameraBackend>();
}

}  // namespace aicad::platform
