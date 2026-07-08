#if defined(__linux__) && !defined(_WIN32)

#include "camera.h"
#include <linux/videodev2.h>
#include <sys/ioctl.h>
#include <fcntl.h>
#include <unistd.h>
#include <sys/mman.h>
#include <cstring>
#include <cerrno>
#include <chrono>
#include <memory>
#include <string>
#include <vector>
#include <algorithm>

namespace aicad::platform {

static uint8_t clamp_u8(int v) {
    return (uint8_t)(v < 0 ? 0 : v > 255 ? 255 : v);
}

class V4L2CameraBackend : public CameraBackend {
    int fd_ = -1;
    v4l2_format fmt_ = {};

public:
    ~V4L2CameraBackend() override { close(); }

    std::vector<CameraInfo> listDevices() override {
        std::vector<CameraInfo> result;
        for (int i = 0; i <= 9; ++i) {
            std::string path = "/dev/video" + std::to_string(i);
            int fd = ::open(path.c_str(), O_RDWR | O_NONBLOCK);
            if (fd < 0) continue;
            v4l2_capability cap = {};
            if (ioctl(fd, VIDIOC_QUERYCAP, &cap) == 0 &&
                (cap.capabilities & V4L2_CAP_VIDEO_CAPTURE)) {
                CameraInfo info;
                info.device_id = path;
                info.label = reinterpret_cast<const char*>(cap.card);
                result.push_back(std::move(info));
            }
            ::close(fd);
        }
        return result;
    }

    bool open(const std::string& device_id) override {
        close();
        fd_ = ::open(device_id.c_str(), O_RDWR | O_NONBLOCK);
        if (fd_ < 0) return false;

        fmt_ = {};
        fmt_.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
        fmt_.fmt.pix.width = 640;
        fmt_.fmt.pix.height = 480;
        fmt_.fmt.pix.pixelformat = V4L2_PIX_FMT_YUYV;
        fmt_.fmt.pix.field = V4L2_FIELD_INTERLACED;

        if (ioctl(fd_, VIDIOC_S_FMT, &fmt_) < 0) {
            // Try MJPEG fallback
            fmt_.fmt.pix.pixelformat = V4L2_PIX_FMT_MJPEG;
            ioctl(fd_, VIDIOC_S_FMT, &fmt_);  // best-effort
        }
        return true;
    }

    void close() override {
        if (fd_ >= 0) { ::close(fd_); fd_ = -1; }
    }

    bool lockExposure(double exposure_us, double gain) override {
        if (fd_ < 0) return false;
        // Best-effort: not all cameras support these controls
        v4l2_control ctrl = {};
        ctrl.id = V4L2_CID_EXPOSURE_ABSOLUTE;
        ctrl.value = (int)(exposure_us / 100.0);  // units: 100us steps
        ioctl(fd_, VIDIOC_S_CTRL, &ctrl);

        ctrl.id = V4L2_CID_GAIN;
        ctrl.value = (int)gain;
        ioctl(fd_, VIDIOC_S_CTRL, &ctrl);

        return true;
    }

    bool grabFrame(CameraFrame& out) override {
        if (fd_ < 0) return false;

        int w = fmt_.fmt.pix.width;
        int h = fmt_.fmt.pix.height;
        size_t frame_size = fmt_.fmt.pix.sizeimage;
        if (frame_size == 0) frame_size = (size_t)w * h * 2;  // YUYV worst-case

        std::vector<uint8_t> buf(frame_size);
        ssize_t n = ::read(fd_, buf.data(), frame_size);
        if (n <= 0) return false;

        out.width = w;
        out.height = h;
        out.rgba.resize(w * h * 4);

        uint32_t pf = fmt_.fmt.pix.pixelformat;
        if (pf == V4L2_PIX_FMT_YUYV) {
            // YUYV: each 4 bytes = 2 pixels (Y0 U Y1 V)
            for (int y = 0; y < h; ++y) {
                for (int x = 0; x < w; x += 2) {
                    int base = (y * w + x) * 2;
                    int Y0 = buf[base + 0];
                    int U  = buf[base + 1];
                    int Y1 = buf[base + 2];
                    int V  = buf[base + 3];

                    auto yuv_to_rgba = [&](int Y, int dst_x) {
                        int i = (y * w + dst_x) * 4;
                        out.rgba[i+0] = clamp_u8(Y + (int)(1.402 * (V - 128)));
                        out.rgba[i+1] = clamp_u8(Y - (int)(0.344 * (U - 128)) - (int)(0.714 * (V - 128)));
                        out.rgba[i+2] = clamp_u8(Y + (int)(1.772 * (U - 128)));
                        out.rgba[i+3] = 255;
                    };
                    yuv_to_rgba(Y0, x);
                    if (x + 1 < w) yuv_to_rgba(Y1, x + 1);
                }
            }
        } else {
            // MJPEG or unknown: fill gray placeholder
            for (int i = 0; i < w * h * 4; i += 4) {
                out.rgba[i+0] = out.rgba[i+1] = out.rgba[i+2] = 128;
                out.rgba[i+3] = 255;
            }
        }

        auto now = std::chrono::steady_clock::now().time_since_epoch();
        out.timestamp_sec = std::chrono::duration<double>(now).count();
        return true;
    }
};

std::unique_ptr<CameraBackend> createV4L2Camera() {
    return std::make_unique<V4L2CameraBackend>();
}

}  // namespace aicad::platform

#endif  // __linux__
