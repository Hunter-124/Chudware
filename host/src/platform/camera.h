#pragma once
#include <memory>
#include <string>
#include <vector>
#include <cstdint>

namespace chudware::platform {

struct CameraInfo {
    std::string device_id;
    std::string label;
};

struct CameraFrame {
    int width = 0;
    int height = 0;
    std::vector<uint8_t> rgba;  // row-major RGBA8
    double timestamp_sec = 0.0;
};

class CameraBackend {
public:
    virtual ~CameraBackend() = default;
    static std::unique_ptr<CameraBackend> create();  // V4L2 / MF / stub

    virtual std::vector<CameraInfo> listDevices() = 0;
    virtual bool open(const std::string& device_id) = 0;
    virtual void close() = 0;
    virtual bool lockExposure(double exposure_us, double gain) = 0;
    virtual bool grabFrame(CameraFrame& out) = 0;  // stills-only; no continuous record
};

}  // namespace chudware::platform
