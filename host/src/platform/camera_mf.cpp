#ifdef _WIN32
#include "camera.h"
// Windows Media Foundation stub — real impl deferred to RE-19 GUI consumer.
// Satisfies the linker on Windows builds; MF headers require SDK.
#include <string>
#include <vector>

namespace aicad::platform {

class MFCameraBackend : public CameraBackend {
    bool opened_ = false;
public:
    std::vector<CameraInfo> listDevices() override { return {}; }
    bool open(const std::string&) override { opened_ = true; return true; }
    void close() override { opened_ = false; }
    bool lockExposure(double, double) override { return false; }
    bool grabFrame(CameraFrame&) override { return false; }
};

std::unique_ptr<CameraBackend> createMFCamera() {
    return std::make_unique<MFCameraBackend>();
}

}  // namespace aicad::platform
#endif  // _WIN32
