// RE-07: CameraBackend stub-backend ctest — pure CPU, no live device.
// Verifies the frozen contract (contracts/reveng.md §7) surface:
//   listDevices -> open -> lockExposure -> grabFrame returns a synthetic RGBA frame.
// The live V4L2 path is exercised separately behind CHUDWARE_TEST_CAMERA=1 (see below).
#include <cassert>
#include <cstdlib>
#include <cstring>
#include <iostream>
#include "../src/platform/camera.h"

using namespace chudware::platform;

// Deterministic stub check: force the synthetic backend so CI has no hardware
// dependency. On Linux, CameraBackend::create() returns V4L2 by default; the
// stub is selected with CHUDWARE_FORCE_STUB_CAMERA=1.
static int test_stub() {
#if defined(__linux__) && !defined(_WIN32)
    ::setenv("CHUDWARE_FORCE_STUB_CAMERA", "1", 1);
    ::unsetenv("CHUDWARE_TEST_CAMERA");
#endif
    auto cam = CameraBackend::create();
    assert(cam && "create() must return a backend");

    auto devices = cam->listDevices();
    assert(!devices.empty() && "stub must advertise at least one device");
    assert(!devices[0].device_id.empty());

    bool opened = cam->open(devices[0].device_id);
    assert(opened && "stub open() must succeed");

    // lockExposure is best-effort; stub returns true.
    bool locked = cam->lockExposure(10000.0, 1.0);
    assert(locked);

    CameraFrame frame;
    bool grabbed = cam->grabFrame(frame);
    assert(grabbed && "stub grabFrame() must return a synthetic frame");

    assert(frame.width > 0 && frame.height > 0);
    assert((int)frame.rgba.size() == frame.width * frame.height * 4);
    // Synthetic frame carries a stamped alpha channel.
    assert(frame.rgba[3] == 255);

    cam->close();
    std::cout << "stub frame: " << frame.width << "x" << frame.height
              << " rgba=" << frame.rgba.size() << " bytes"
              << " ts=" << frame.timestamp_sec << "\n";
    return 0;
}

// Optional live-device smoke, only when CHUDWARE_TEST_CAMERA=1 is set by the caller.
// Never fails CI when no camera is attached: open()/grab() falling back is tolerated.
static int test_live_optional(bool live_requested) {
    if (!live_requested) {
        std::cout << "live smoke skipped (CHUDWARE_TEST_CAMERA != 1)\n";
        return 0;
    }
#if defined(__linux__) && !defined(_WIN32)
    ::unsetenv("CHUDWARE_FORCE_STUB_CAMERA");  // real V4L2 path
    ::setenv("CHUDWARE_TEST_CAMERA", "1", 1);
#endif
    auto cam = CameraBackend::create();
    auto devices = cam->listDevices();
    if (devices.empty()) {
        std::cout << "live smoke: no /dev/video* devices — tolerated\n";
        return 0;
    }
    if (!cam->open(devices[0].device_id)) {
        std::cout << "live smoke: open() failed on " << devices[0].device_id
                  << " — tolerated\n";
        return 0;
    }
    cam->lockExposure(10000.0, 1.0);
    CameraFrame frame;
    if (cam->grabFrame(frame)) {
        std::cout << "live frame: " << frame.width << "x" << frame.height << "\n";
    } else {
        std::cout << "live smoke: grabFrame() returned false — tolerated\n";
    }
    cam->close();
    return 0;
}

int main() {
    const char* ev = std::getenv("CHUDWARE_TEST_CAMERA");
    bool live_requested = ev && std::strcmp(ev, "1") == 0;
    if (int rc = test_stub(); rc != 0) return rc;
    if (int rc = test_live_optional(live_requested); rc != 0) return rc;
    std::cout << "PASS\n";
    return 0;
}
