#if defined(_WIN32)
// ---------------------------------------------------------------------------------------------
// camera_mf.cpp — Windows Media Foundation implementation of CameraBackend (camera.h v2).
//
// Peer of camera_v4l2.cpp: enumerate devices + native modes, negotiate the highest useful mode,
// stream RGBA frames, and expose the focus / exposure / white-balance / gain controls that
// photogrammetry needs frozen before a capture run (see lockForCapture).
//
// Decode policy: the source reader is created with MF_SOURCE_READER_ENABLE_VIDEO_PROCESSING so
// MF's own converter turns MJPG / NV12 / YUY2 into RGB32 for us. Drivers that refuse RGB32 fall
// back to NV12, then YUY2, then the native subtype, decoded by the hand-written converters
// below — so a camera that only speaks NV12 still works without dragging a JPEG decoder in-tree.
//
// Link dependencies are declared here with #pragma comment(lib, …) rather than in
// host/CMakeLists.txt: this is the only translation unit that touches Media Foundation, and the
// shared CMake file is cross-platform.
// ---------------------------------------------------------------------------------------------
#include "camera.h"

#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#include <objbase.h>
#include <mfapi.h>
#include <mfidl.h>
#include <mfobjects.h>
#include <mfreadwrite.h>
#include <mferror.h>
#include <strmif.h>  // IAMCameraControl / IAMVideoProcAmp and their property + flag enums

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <memory>
#include <mutex>
#include <string>
#include <vector>

#pragma comment(lib, "mfplat.lib")
#pragma comment(lib, "mf.lib")
#pragma comment(lib, "mfreadwrite.lib")
#pragma comment(lib, "mfuuid.lib")
#pragma comment(lib, "strmiids.lib")
#pragma comment(lib, "ole32.lib")

// Present since the Win8 SDK; spelled out so an older toolchain still builds.
#ifndef MF_E_VIDEO_RECORDING_DEVICE_INVALIDATED
#define MF_E_VIDEO_RECORDING_DEVICE_INVALIDATED ((HRESULT)0xC00D3E85L)
#endif

namespace chudware::platform {
namespace {

// ---- minimal COM smart pointer -----------------------------------------------------------------
// Deliberately local rather than pulling in <wrl/client.h>: the repo does not otherwise depend on
// WRL, and this is the whole of what we need from it.
template <class T>
class ComRef {
    T* p_ = nullptr;

public:
    ComRef() = default;
    ComRef(const ComRef& o) : p_(o.p_) { if (p_) p_->AddRef(); }
    ComRef(ComRef&& o) noexcept : p_(o.p_) { o.p_ = nullptr; }
    ~ComRef() { reset(); }

    ComRef& operator=(const ComRef& o) {
        if (this != &o) { T* q = o.p_; if (q) q->AddRef(); reset(); p_ = q; }
        return *this;
    }
    ComRef& operator=(ComRef&& o) noexcept {
        if (this != &o) { reset(); p_ = o.p_; o.p_ = nullptr; }
        return *this;
    }

    void reset() { if (p_) { p_->Release(); p_ = nullptr; } }
    T** put() { reset(); return &p_; }
    void** putVoid() { reset(); return reinterpret_cast<void**>(&p_); }
    T* get() const { return p_; }
    T* operator->() const { return p_; }
    explicit operator bool() const { return p_ != nullptr; }

    // Takes ownership of an already-AddRef'd pointer.
    void attach(T* raw) { reset(); p_ = raw; }
};

// ---- process/thread COM + MF lifetime -----------------------------------------------------------

// MF objects are apartment-bound; grabFrame() may be pumped from the GL thread while
// listDevices() runs on the UI thread, so every thread that reaches MF initialises COM once and
// tears it down when the thread exits.
struct ComApartment {
    bool owned = false;
    ComApartment() {
        // S_FALSE (already initialised on this thread) still requires a balancing CoUninitialize;
        // RPC_E_CHANGED_MODE means somebody else picked the apartment model and we must not.
        const HRESULT hr = ::CoInitializeEx(nullptr, COINIT_APARTMENTTHREADED | COINIT_DISABLE_OLE1DDE);
        owned = SUCCEEDED(hr);
    }
    ~ComApartment() { if (owned) ::CoUninitialize(); }
};

void ensureComApartment() {
    thread_local ComApartment apartment;
    (void)apartment;
}

// MFStartup/MFShutdown are refcounted here so several backends (GUI preview + a headless probe)
// can coexist without one's teardown pulling the platform out from under the other.
class MFPlatform {
    static std::mutex mtx_;
    static int refs_;
    static bool started_;

public:
    static bool acquire() {
        std::lock_guard<std::mutex> lock(mtx_);
        if (refs_ == 0) started_ = SUCCEEDED(::MFStartup(MF_VERSION, MFSTARTUP_FULL));
        ++refs_;
        return started_;
    }
    static void release() {
        std::lock_guard<std::mutex> lock(mtx_);
        if (refs_ > 0 && --refs_ == 0 && started_) {
            ::MFShutdown();
            started_ = false;
        }
    }
};
std::mutex MFPlatform::mtx_;
int MFPlatform::refs_ = 0;
bool MFPlatform::started_ = false;

// ---- small helpers --------------------------------------------------------------------------

std::string wideToUtf8(const wchar_t* w, UINT32 len) {
    if (!w || len == 0) return {};
    const int need = ::WideCharToMultiByte(CP_UTF8, 0, w, (int)len, nullptr, 0, nullptr, nullptr);
    if (need <= 0) return {};
    std::string out((size_t)need, '\0');
    ::WideCharToMultiByte(CP_UTF8, 0, w, (int)len, &out[0], need, nullptr, nullptr);
    return out;
}

std::string hresultHex(HRESULT hr) {
    char buf[16];
    std::snprintf(buf, sizeof(buf), "0x%08lX", (unsigned long)hr);
    return buf;
}

uint8_t clampU8(int v) {
    return (uint8_t)(v < 0 ? 0 : (v > 255 ? 255 : v));
}

// The symbolic link is
//   \\?\usb#vid_046d&pid_0825&mi_00#6&2f8a1b1e&0&0000#{e5323777-…}\global
// The portion before the interface-class GUID is the device instance path, which is stable for a
// given physical USB port — exactly what CameraInfo::bus is for.
std::string busFromSymlink(const std::string& symlink) {
    if (symlink.empty()) return {};
    size_t begin = 0;
    if (symlink.rfind("\\\\?\\", 0) == 0) begin = 4;
    const size_t guid = symlink.find("#{", begin);
    const std::string bus =
        symlink.substr(begin, guid == std::string::npos ? std::string::npos : guid - begin);
    return bus.empty() ? symlink : bus;
}

// `fourcc` is documented as the opaque platform tag. For MF that is the subtype GUID's Data1,
// which really is the FourCC for the YUV/compressed families and a D3DFMT_* ordinal for RGB.
struct SubtypeDesc {
    std::string label;
    bool compressed = false;
};

SubtypeDesc describeSubtype(const GUID& sub) {
    struct Entry { const GUID* guid; const char* label; bool compressed; };
    static const Entry kTable[] = {
        {&MFVideoFormat_MJPG,   "MJPG",   true },
        {&MFVideoFormat_H264,   "H264",   true },
        {&MFVideoFormat_NV12,   "NV12",   false},
        {&MFVideoFormat_YUY2,   "YUY2",   false},
        {&MFVideoFormat_UYVY,   "UYVY",   false},
        {&MFVideoFormat_YV12,   "YV12",   false},
        {&MFVideoFormat_I420,   "I420",   false},
        {&MFVideoFormat_RGB32,  "RGB32",  false},
        {&MFVideoFormat_ARGB32, "ARGB32", false},
        {&MFVideoFormat_RGB24,  "RGB24",  false},
    };
    for (const Entry& e : kTable)
        if (::IsEqualGUID(sub, *e.guid)) return {e.label, e.compressed};

    // Unknown subtype: surface the raw tag rather than inventing a name.
    char buf[16];
    const uint32_t tag = sub.Data1;
    if (tag > 0x20202020u) {
        std::snprintf(buf, sizeof(buf), "%c%c%c%c", (char)(tag & 0xFF), (char)((tag >> 8) & 0xFF),
                      (char)((tag >> 16) & 0xFF), (char)((tag >> 24) & 0xFF));
    } else {
        std::snprintf(buf, sizeof(buf), "fmt%u", (unsigned)tag);
    }
    return {buf, false};
}

// ---- pixel conversion -------------------------------------------------------------------------
// All of these write tightly-packed RGBA8 (stride == width*4) as CameraFrame requires.
// `row_step` is the signed byte delta between consecutive source rows, so a bottom-up DIB
// (negative MF_MT_DEFAULT_STRIDE) is handled by walking backwards from the top scanline.

void convertBgr32(const uint8_t* top, ptrdiff_t row_step, int w, int h, uint8_t* dst) {
    for (int y = 0; y < h; ++y) {
        const uint8_t* src = top + (ptrdiff_t)y * row_step;
        uint8_t* out = dst + (size_t)y * (size_t)w * 4u;
        for (int x = 0; x < w; ++x) {
            out[0] = src[2];  // MFVideoFormat_RGB32 is X8R8G8B8 => B,G,R,X in memory
            out[1] = src[1];
            out[2] = src[0];
            out[3] = 255;
            src += 4;
            out += 4;
        }
    }
}

void convertBgr24(const uint8_t* top, ptrdiff_t row_step, int w, int h, uint8_t* dst) {
    for (int y = 0; y < h; ++y) {
        const uint8_t* src = top + (ptrdiff_t)y * row_step;
        uint8_t* out = dst + (size_t)y * (size_t)w * 4u;
        for (int x = 0; x < w; ++x) {
            out[0] = src[2];
            out[1] = src[1];
            out[2] = src[0];
            out[3] = 255;
            src += 3;
            out += 4;
        }
    }
}

// BT.601 limited-range YCbCr -> RGB, integer form.
inline void yuvToRgba(int y, int u, int v, uint8_t* out) {
    const int c = y - 16;
    const int d = u - 128;
    const int e = v - 128;
    out[0] = clampU8((298 * c + 409 * e + 128) >> 8);
    out[1] = clampU8((298 * c - 100 * d - 208 * e + 128) >> 8);
    out[2] = clampU8((298 * c + 516 * d + 128) >> 8);
    out[3] = 255;
}

// NV12: full-res Y plane, then a half-height plane of interleaved Cb,Cr at the same pitch.
void convertNv12(const uint8_t* top, ptrdiff_t row_step, int w, int h, uint8_t* dst) {
    const uint8_t* uv_top = top + row_step * (ptrdiff_t)h;
    for (int y = 0; y < h; ++y) {
        const uint8_t* yrow = top + (ptrdiff_t)y * row_step;
        const uint8_t* uvrow = uv_top + (ptrdiff_t)(y / 2) * row_step;
        uint8_t* out = dst + (size_t)y * (size_t)w * 4u;
        for (int x = 0; x < w; ++x) {
            const int cx = (x & ~1);
            yuvToRgba(yrow[x], uvrow[cx], uvrow[cx + 1], out);
            out += 4;
        }
    }
}

// YUY2: 4 bytes per 2 pixels — Y0 Cb Y1 Cr.
void convertYuy2(const uint8_t* top, ptrdiff_t row_step, int w, int h, uint8_t* dst) {
    for (int y = 0; y < h; ++y) {
        const uint8_t* src = top + (ptrdiff_t)y * row_step;
        uint8_t* out = dst + (size_t)y * (size_t)w * 4u;
        int x = 0;
        for (; x + 1 < w; x += 2) {
            yuvToRgba(src[0], src[1], src[3], out);
            yuvToRgba(src[2], src[1], src[3], out + 4);
            src += 4;
            out += 8;
        }
        if (x < w)  // odd width: replay the last chroma pair for the trailing luma
            yuvToRgba(src[0], src[1], src[3], out);
    }
}

// ---- locked media buffer ------------------------------------------------------------------------
// Prefers IMF2DBuffer2/IMF2DBuffer because only those report the true pitch; falls back to a flat
// Lock() with the media type's default stride.
class BufferLock {
    ComRef<IMF2DBuffer2> b2d2_;
    ComRef<IMF2DBuffer> b2d_;
    ComRef<IMFMediaBuffer> flat_;
    BYTE* scanline0_ = nullptr;
    LONG pitch_ = 0;

public:
    BufferLock() = default;
    BufferLock(const BufferLock&) = delete;
    BufferLock& operator=(const BufferLock&) = delete;

    ~BufferLock() {
        if (b2d2_) b2d2_->Unlock2D();
        else if (b2d_) b2d_->Unlock2D();
        else if (flat_) flat_->Unlock();
    }

    // `fallback_stride` is the signed MF_MT_DEFAULT_STRIDE, used only on the flat path.
    bool lock(IMFMediaBuffer* buffer, LONG fallback_stride, int height) {
        BYTE* start = nullptr;
        DWORD length = 0;
        if (SUCCEEDED(buffer->QueryInterface(IID_PPV_ARGS(b2d2_.put())))) {
            if (SUCCEEDED(b2d2_->Lock2DSize(MF2DBuffer_LockFlags_Read, &scanline0_, &pitch_, &start,
                                            &length)))
                return true;
            b2d2_.reset();
        }
        if (SUCCEEDED(buffer->QueryInterface(IID_PPV_ARGS(b2d_.put())))) {
            if (SUCCEEDED(b2d_->Lock2D(&scanline0_, &pitch_))) return true;
            b2d_.reset();
        }

        DWORD max_len = 0, cur_len = 0;
        BYTE* data = nullptr;
        if (FAILED(buffer->Lock(&data, &max_len, &cur_len))) return false;
        buffer->AddRef();
        flat_.attach(buffer);
        pitch_ = fallback_stride;
        // A negative default stride means the buffer is a bottom-up DIB: byte 0 is the LAST row,
        // so the top scanline lives at the far end and we walk backwards.
        scanline0_ = (pitch_ < 0) ? data + (ptrdiff_t)(height - 1) * (ptrdiff_t)(-pitch_) : data;
        return true;
    }

    const uint8_t* top() const { return scanline0_; }
    ptrdiff_t rowStep() const { return (ptrdiff_t)pitch_; }
};

// ---- device enumeration ---------------------------------------------------------------------

// Owns the IMFActivate array handed back by MFEnumDeviceSources.
class ActivateList {
    IMFActivate** items_ = nullptr;
    UINT32 count_ = 0;

public:
    ActivateList() = default;
    ActivateList(const ActivateList&) = delete;
    ActivateList& operator=(const ActivateList&) = delete;
    ~ActivateList() { clear(); }

    void clear() {
        if (items_) {
            for (UINT32 i = 0; i < count_; ++i)
                if (items_[i]) items_[i]->Release();
            ::CoTaskMemFree(items_);
            items_ = nullptr;
        }
        count_ = 0;
    }

    HRESULT enumerate() {
        clear();
        ComRef<IMFAttributes> attrs;
        HRESULT hr = ::MFCreateAttributes(attrs.put(), 1);
        if (FAILED(hr)) return hr;
        hr = attrs->SetGUID(MF_DEVSOURCE_ATTRIBUTE_SOURCE_TYPE,
                            MF_DEVSOURCE_ATTRIBUTE_SOURCE_TYPE_VIDCAP_GUID);
        if (FAILED(hr)) return hr;
        return ::MFEnumDeviceSources(attrs.get(), &items_, &count_);
    }

    UINT32 size() const { return count_; }
    IMFActivate* at(UINT32 i) const { return items_[i]; }

    static std::string stringAttr(IMFActivate* act, const GUID& key) {
        WCHAR* value = nullptr;
        UINT32 len = 0;
        if (FAILED(act->GetAllocatedString(key, &value, &len))) return {};
        std::string out = wideToUtf8(value, len);
        ::CoTaskMemFree(value);
        return out;
    }

    static std::string symlinkOf(IMFActivate* act) {
        return stringAttr(act, MF_DEVSOURCE_ATTRIBUTE_SOURCE_TYPE_VIDCAP_SYMBOLIC_LINK);
    }
};

// One enumerated native capture format plus the reader index it came from.
struct NativeMode {
    CameraMode mode;
    DWORD index = 0;
};

bool modeIsBetter(const CameraMode& a, const CameraMode& b) {
    const long long pa = (long long)a.width * a.height;
    const long long pb = (long long)b.width * b.height;
    if (pa != pb) return pa > pb;
    return a.fps > b.fps;
}

}  // namespace

// =================================================================================================

class MFCameraBackend final : public CameraBackend {
    ComRef<IMFMediaSource> source_;
    ComRef<IMFSourceReader> reader_;
    ComRef<IAMCameraControl> cam_ctrl_;
    ComRef<IAMVideoProcAmp> proc_amp_;

    CameraMode active_{};
    std::string device_id_;
    std::string last_error_;
    bool opened_ = false;
    bool mf_ready_ = false;

    // Negotiated output format that grabFrame() must decode.
    enum class Decode { None, Bgr32, Bgr24, Nv12, Yuy2 };
    Decode decode_ = Decode::None;
    LONG default_stride_ = 0;
    int out_w_ = 0;
    int out_h_ = 0;

public:
    MFCameraBackend() {
        ensureComApartment();
        mf_ready_ = MFPlatform::acquire();
        if (!mf_ready_)
            last_error_ = "MFStartup failed — Media Foundation unavailable on this system";
    }

    ~MFCameraBackend() override {
        close();
        MFPlatform::release();
    }

    // ---- lifecycle ------------------------------------------------------------------------

    std::vector<CameraInfo> listDevices() override {
        std::vector<CameraInfo> out;
        if (!requireMF()) return out;
        ensureComApartment();

        ActivateList list;
        const HRESULT hr = list.enumerate();
        if (FAILED(hr)) {
            fail("MFEnumDeviceSources", hr);
            return out;
        }

        out.reserve(list.size());
        for (UINT32 i = 0; i < list.size(); ++i) {
            IMFActivate* act = list.at(i);
            CameraInfo info;
            info.device_id = ActivateList::symlinkOf(act);
            if (info.device_id.empty()) continue;  // unaddressable without a symbolic link
            info.label = ActivateList::stringAttr(act, MF_DEVSOURCE_ATTRIBUTE_FRIENDLY_NAME);
            if (info.label.empty()) info.label = "Camera " + std::to_string(i);
            info.bus = busFromSymlink(info.device_id);
            out.push_back(std::move(info));
        }
        if (out.empty()) last_error_ = "no video capture devices found";
        return out;
    }

    std::vector<CameraMode> listModes(const std::string& device_id) override {
        std::vector<CameraMode> out;
        if (!requireMF()) return out;
        ensureComApartment();

        ComRef<IMFMediaSource> source;
        if (!activateSource(device_id, source)) return out;

        ComRef<IMFSourceReader> reader;
        const HRESULT hr =
            ::MFCreateSourceReaderFromMediaSource(source.get(), nullptr, reader.put());
        if (FAILED(hr)) {
            fail("MFCreateSourceReaderFromMediaSource", hr);
            source->Shutdown();
            return out;
        }

        std::vector<NativeMode> natives;
        enumerateNative(reader.get(), natives);
        reader.reset();
        source->Shutdown();  // release the device; the GUI only wanted the mode list

        out.reserve(natives.size());
        for (const NativeMode& n : natives) out.push_back(n.mode);
        if (out.empty()) last_error_ = "device exposed no native video media types";
        return out;
    }

    bool open(const std::string& device_id, const OpenRequest& req) override {
        close();
        if (!requireMF()) return false;
        ensureComApartment();

        ComRef<IMFMediaSource> source;
        if (!activateSource(device_id, source)) return false;

        // ENABLE_VIDEO_PROCESSING lets the reader insert MF's colour converter / MJPG decoder, so
        // we can simply ask for RGB32 on most devices instead of decoding by hand.
        ComRef<IMFAttributes> reader_attrs;
        HRESULT hr = ::MFCreateAttributes(reader_attrs.put(), 1);
        if (SUCCEEDED(hr)) reader_attrs->SetUINT32(MF_SOURCE_READER_ENABLE_VIDEO_PROCESSING, TRUE);

        ComRef<IMFSourceReader> reader;
        hr = ::MFCreateSourceReaderFromMediaSource(source.get(), reader_attrs.get(), reader.put());
        if (FAILED(hr)) {
            fail("MFCreateSourceReaderFromMediaSource", hr);
            source->Shutdown();
            return false;
        }

        std::vector<NativeMode> natives;
        enumerateNative(reader.get(), natives);
        if (natives.empty()) {
            last_error_ = "device exposed no native video media types";
            source->Shutdown();
            return false;
        }

        const NativeMode& chosen = natives[pickMode(natives, req)];
        ComRef<IMFMediaType> native_type;
        hr = reader->GetNativeMediaType(MF_SOURCE_READER_FIRST_VIDEO_STREAM, chosen.index,
                                        native_type.put());
        if (FAILED(hr)) {
            fail("GetNativeMediaType", hr);
            source->Shutdown();
            return false;
        }

        // Lock the sensor-side resolution/rate first…
        hr = reader->SetCurrentMediaType(MF_SOURCE_READER_FIRST_VIDEO_STREAM, nullptr,
                                         native_type.get());
        if (FAILED(hr)) {
            fail("SetCurrentMediaType(native)", hr);
            source->Shutdown();
            return false;
        }

        // …then ask the reader's processor for something we can blit. RGB32 first (MF does the
        // MJPEG/NV12 decode), degrading to formats our own converters handle.
        const GUID* wanted[] = {&MFVideoFormat_RGB32, &MFVideoFormat_NV12, &MFVideoFormat_YUY2};
        for (const GUID* want : wanted) {
            ComRef<IMFMediaType> out_type;
            if (FAILED(::MFCreateMediaType(out_type.put()))) continue;
            if (FAILED(native_type->CopyAllItems(out_type.get()))) continue;
            out_type->SetGUID(MF_MT_SUBTYPE, *want);
            // These describe the *native* buffer; leaving them would contradict the new subtype.
            out_type->DeleteItem(MF_MT_DEFAULT_STRIDE);
            out_type->DeleteItem(MF_MT_SAMPLE_SIZE);
            out_type->DeleteItem(MF_MT_COMPRESSED);
            out_type->DeleteItem(MF_MT_ALL_SAMPLES_INDEPENDENT);
            if (SUCCEEDED(reader->SetCurrentMediaType(MF_SOURCE_READER_FIRST_VIDEO_STREAM, nullptr,
                                                      out_type.get())))
                break;
        }

        ComRef<IMFMediaType> current;
        hr = reader->GetCurrentMediaType(MF_SOURCE_READER_FIRST_VIDEO_STREAM, current.put());
        if (FAILED(hr)) {
            fail("GetCurrentMediaType", hr);
            source->Shutdown();
            return false;
        }

        GUID out_sub = GUID_NULL;
        current->GetGUID(MF_MT_SUBTYPE, &out_sub);
        const Decode decode = decodeFor(out_sub);
        if (decode == Decode::None) {
            last_error_ = "no usable output format: device offers only " +
                          describeSubtype(out_sub).label +
                          ", which this build cannot decode (MF video processing unavailable)";
            source->Shutdown();
            return false;
        }

        UINT32 cw = 0, ch = 0;
        if (FAILED(::MFGetAttributeSize(current.get(), MF_MT_FRAME_SIZE, &cw, &ch)) || cw == 0 ||
            ch == 0) {
            last_error_ = "negotiated media type has no frame size";
            source->Shutdown();
            return false;
        }

        // Stride: prefer what the type declares (it carries the bottom-up sign), else derive it.
        LONG stride = 0;
        UINT32 declared = 0;
        if (SUCCEEDED(current->GetUINT32(MF_MT_DEFAULT_STRIDE, &declared))) {
            stride = (LONG)(INT32)declared;
        } else if (FAILED(::MFGetStrideForBitmapInfoHeader(out_sub.Data1, cw, &stride))) {
            stride = (LONG)(cw * bytesPerPixel(decode));
        }

        reader->SetStreamSelection(MF_SOURCE_READER_ALL_STREAMS, FALSE);
        reader->SetStreamSelection(MF_SOURCE_READER_FIRST_VIDEO_STREAM, TRUE);

        source_ = std::move(source);
        reader_ = std::move(reader);
        decode_ = decode;
        default_stride_ = stride;
        out_w_ = (int)cw;
        out_h_ = (int)ch;
        device_id_ = device_id;

        // activeMode() reports the sensor-side (wire) format, matching what listModes() offers,
        // but with the frame size/rate actually negotiated.
        active_ = chosen.mode;
        active_.width = (int)cw;
        active_.height = (int)ch;
        UINT32 num = 0, den = 0;
        if (SUCCEEDED(::MFGetAttributeRatio(current.get(), MF_MT_FRAME_RATE, &num, &den)) && den)
            active_.fps = (double)num / (double)den;

        bindControlInterfaces();
        opened_ = true;
        last_error_.clear();
        return true;
    }

    void close() override {
        cam_ctrl_.reset();
        proc_amp_.reset();
        reader_.reset();
        if (source_) {
            source_->Shutdown();  // hand the device back; otherwise it stays busy process-wide
            source_.reset();
        }
        opened_ = false;
        decode_ = Decode::None;
        default_stride_ = 0;
        out_w_ = out_h_ = 0;
        active_ = CameraMode{};
        device_id_.clear();
    }

    bool isOpen() const override { return opened_; }
    CameraMode activeMode() const override { return active_; }
    std::string lastError() const override { return last_error_; }

    // ---- streaming ------------------------------------------------------------------------

    GrabResult grabFrame(CameraFrame& out) override {
        if (!opened_ || !reader_) return GrabResult::not_open;
        ensureComApartment();

        DWORD stream = 0, flags = 0;
        LONGLONG timestamp = 0;
        ComRef<IMFSample> sample;
        // Control flags 0: no DRAIN, no FLUSH — the plain "give me the next frame" read. The
        // synchronous reader returns as soon as the driver has a buffer and hands back a NULL
        // sample (flags set, no error) when it has nothing yet — that is `again`, not a fault.
        const HRESULT hr = reader_->ReadSample(MF_SOURCE_READER_FIRST_VIDEO_STREAM, 0, &stream,
                                               &flags, &timestamp, sample.put());
        if (FAILED(hr)) {
            fail("ReadSample", hr);
            return isDeviceLost(hr) ? GrabResult::device_lost : GrabResult::decode_failed;
        }
        if (flags & MF_SOURCE_READERF_ERROR) {
            last_error_ = "ReadSample reported MF_SOURCE_READERF_ERROR (device removed?)";
            return GrabResult::device_lost;
        }
        if (flags & MF_SOURCE_READERF_ENDOFSTREAM) {
            last_error_ = "capture stream ended (device unplugged or claimed by another app)";
            return GrabResult::device_lost;
        }
        if (flags & MF_SOURCE_READERF_CURRENTMEDIATYPECHANGED) {
            if (!refreshCurrentType()) return GrabResult::decode_failed;
        }
        if (!sample) return GrabResult::again;

        ComRef<IMFMediaBuffer> buffer;
        if (FAILED(sample->ConvertToContiguousBuffer(buffer.put()))) {
            last_error_ = "ConvertToContiguousBuffer failed on a capture sample";
            return GrabResult::decode_failed;
        }

        BufferLock lock;
        if (!lock.lock(buffer.get(), default_stride_, out_h_)) {
            last_error_ = "could not lock the capture buffer";
            return GrabResult::decode_failed;
        }
        if (!lock.top()) {
            last_error_ = "capture buffer locked with a null scanline pointer";
            return GrabResult::decode_failed;
        }

        out.width = out_w_;
        out.height = out_h_;
        out.rgba.resize((size_t)out_w_ * (size_t)out_h_ * 4u);
        const ptrdiff_t step = lock.rowStep();
        switch (decode_) {
            case Decode::Bgr32: convertBgr32(lock.top(), step, out_w_, out_h_, out.rgba.data()); break;
            case Decode::Bgr24: convertBgr24(lock.top(), step, out_w_, out_h_, out.rgba.data()); break;
            case Decode::Nv12:  convertNv12(lock.top(), step, out_w_, out_h_, out.rgba.data());  break;
            case Decode::Yuy2:  convertYuy2(lock.top(), step, out_w_, out_h_, out.rgba.data());  break;
            case Decode::None:  return GrabResult::decode_failed;
        }

        out.synthetic = false;                        // real photons
        out.timestamp_sec = (double)timestamp / 1e7;  // MF presentation time is 100-ns units
        out.sharpness = -1.0;                         // unscored; frameSharpness() is the caller's call
        last_error_.clear();
        return GrabResult::ok;
    }

    // ---- controls -------------------------------------------------------------------------

    ControlState getControl(CameraControl c) override {
        ControlState st;
        long min = 0, max = 0, step = 0, def = 0, caps = 0, value = 0, flags = 0;
        if (!readControl(c, min, max, step, def, caps, value, flags)) return st;

        st.supported = true;
        st.auto_capable = (caps & CameraControl_Flags_Auto) != 0;
        st.automatic = (flags & CameraControl_Flags_Auto) != 0;
        st.value = (max > min) ? (double)(value - min) / (double)(max - min) : 0.0;
        st.value = std::min(1.0, std::max(0.0, st.value));
        return st;
    }

    bool setControl(CameraControl c, double normalized_value) override {
        long min = 0, max = 0, step = 0, def = 0, caps = 0, value = 0, flags = 0;
        if (!readControl(c, min, max, step, def, caps, value, flags)) return false;

        const double clamped = std::min(1.0, std::max(0.0, normalized_value));
        long raw = min + (long)std::lround(clamped * (double)(max - min));
        if (step > 1) raw = min + ((raw - min) / step) * step;  // snap to the driver's grid
        raw = std::min(max, std::max(min, raw));
        return writeControl(c, raw, /*automatic=*/false);
    }

    bool setControlAuto(CameraControl c, bool automatic) override {
        long min = 0, max = 0, step = 0, def = 0, caps = 0, value = 0, flags = 0;
        if (!readControl(c, min, max, step, def, caps, value, flags)) return false;
        if (automatic && (caps & CameraControl_Flags_Auto) == 0) {
            last_error_ = std::string(controlName(c)) + ": device does not support automatic mode";
            return false;
        }
        // Keep the present value; only the mode flag changes.
        return writeControl(c, value, automatic);
    }

    // The photogrammetry pre-flight: let the camera converge, then pin every intrinsic-affecting
    // control to whatever it converged on, so focal length and colour response hold across the set.
    bool lockForCapture() override {
        if (!opened_) {
            last_error_ = "lockForCapture called with no open device";
            return false;
        }
        static const CameraControl kFreeze[] = {CameraControl::Focus, CameraControl::Exposure,
                                                CameraControl::WhiteBalance, CameraControl::Gain};
        bool all_locked = true;
        std::string failures;
        for (CameraControl c : kFreeze) {
            long min = 0, max = 0, step = 0, def = 0, caps = 0, value = 0, flags = 0;
            if (!readControl(c, min, max, step, def, caps, value, flags))
                continue;  // unsupported controls are not failures
            if (!writeControl(c, value, /*automatic=*/false)) {
                all_locked = false;
                if (!failures.empty()) failures += ", ";
                failures += controlName(c);
            }
        }
        if (!all_locked)
            last_error_ = "could not switch to manual: " + failures;
        else
            last_error_.clear();
        return all_locked;
    }

private:
    // ---- error plumbing ---------------------------------------------------------------------

    void fail(const char* call, HRESULT hr) {
        last_error_ = std::string(call) + " failed (" + hresultHex(hr) + ")";
    }

    bool requireMF() {
        if (!mf_ready_) {
            last_error_ = "Media Foundation is not initialised (MFStartup failed)";
            return false;
        }
        return true;
    }

    static bool isDeviceLost(HRESULT hr) {
        return hr == MF_E_VIDEO_RECORDING_DEVICE_INVALIDATED ||
               hr == MF_E_HW_MFT_FAILED_START_STREAMING ||
               hr == HRESULT_FROM_WIN32(ERROR_DEVICE_REMOVED);
    }

    static const char* controlName(CameraControl c) {
        switch (c) {
            case CameraControl::Focus:        return "focus";
            case CameraControl::Exposure:     return "exposure";
            case CameraControl::WhiteBalance: return "white balance";
            case CameraControl::Gain:         return "gain";
            case CameraControl::Zoom:         return "zoom";
            case CameraControl::Brightness:   return "brightness";
        }
        return "control";
    }

    static unsigned bytesPerPixel(Decode d) {
        switch (d) {
            case Decode::Bgr32: return 4;
            case Decode::Bgr24: return 3;
            case Decode::Yuy2:  return 2;
            case Decode::Nv12:  return 1;  // Y-plane pitch
            case Decode::None:  return 0;
        }
        return 0;
    }

    static Decode decodeFor(const GUID& sub) {
        if (::IsEqualGUID(sub, MFVideoFormat_RGB32) || ::IsEqualGUID(sub, MFVideoFormat_ARGB32))
            return Decode::Bgr32;
        if (::IsEqualGUID(sub, MFVideoFormat_RGB24)) return Decode::Bgr24;
        if (::IsEqualGUID(sub, MFVideoFormat_NV12)) return Decode::Nv12;
        if (::IsEqualGUID(sub, MFVideoFormat_YUY2)) return Decode::Yuy2;
        return Decode::None;
    }

    // ---- source / mode plumbing ---------------------------------------------------------------

    // Resolves `device_id` (a symbolic link; empty = first device) and activates its media source.
    bool activateSource(const std::string& device_id, ComRef<IMFMediaSource>& out) {
        ActivateList list;
        HRESULT hr = list.enumerate();
        if (FAILED(hr)) {
            fail("MFEnumDeviceSources", hr);
            return false;
        }
        if (list.size() == 0) {
            last_error_ = "no video capture devices found";
            return false;
        }

        IMFActivate* match = nullptr;
        if (device_id.empty()) {
            match = list.at(0);
        } else {
            for (UINT32 i = 0; i < list.size() && !match; ++i)
                if (ActivateList::symlinkOf(list.at(i)) == device_id) match = list.at(i);
        }
        if (!match) {
            last_error_ = "no capture device matches id '" + device_id + "'";
            return false;
        }

        hr = match->ActivateObject(IID_PPV_ARGS(out.put()));
        if (FAILED(hr)) {
            // The overwhelmingly common causes are another process holding the camera and the
            // Windows camera-privacy setting blocking desktop apps.
            fail("IMFActivate::ActivateObject (device busy or blocked by privacy settings)", hr);
            return false;
        }
        return true;
    }

    void enumerateNative(IMFSourceReader* reader, std::vector<NativeMode>& out) {
        for (DWORD i = 0;; ++i) {
            ComRef<IMFMediaType> type;
            const HRESULT hr =
                reader->GetNativeMediaType(MF_SOURCE_READER_FIRST_VIDEO_STREAM, i, type.put());
            if (hr == MF_E_NO_MORE_TYPES) break;
            if (FAILED(hr)) break;

            UINT32 w = 0, h = 0;
            if (FAILED(::MFGetAttributeSize(type.get(), MF_MT_FRAME_SIZE, &w, &h)) || w == 0 ||
                h == 0)
                continue;

            GUID sub = GUID_NULL;
            if (FAILED(type->GetGUID(MF_MT_SUBTYPE, &sub))) continue;

            UINT32 num = 0, den = 0;
            double fps = 0.0;
            if (SUCCEEDED(::MFGetAttributeRatio(type.get(), MF_MT_FRAME_RATE, &num, &den)) && den)
                fps = (double)num / (double)den;

            const SubtypeDesc desc = describeSubtype(sub);
            CameraMode m;
            m.width = (int)w;
            m.height = (int)h;
            m.fps = fps;
            m.fourcc = (uint32_t)sub.Data1;
            m.format_label = desc.label;
            m.compressed = desc.compressed;

            // Drivers routinely repeat an identical descriptor across stream indices.
            const bool dup = std::any_of(out.begin(), out.end(), [&](const NativeMode& n) {
                return n.mode.width == m.width && n.mode.height == m.height &&
                       n.mode.fourcc == m.fourcc && std::fabs(n.mode.fps - m.fps) < 0.01;
            });
            if (!dup) out.push_back(NativeMode{m, i});
        }

        std::sort(out.begin(), out.end(), [](const NativeMode& a, const NativeMode& b) {
            return modeIsBetter(a.mode, b.mode);
        });
    }

    // Same policy as the V4L2 backend: an explicit size wins and picks the closest match;
    // otherwise photogrammetry's default is the largest sensor area available.
    static size_t pickMode(const std::vector<NativeMode>& modes, const OpenRequest& req) {
        size_t best = 0;
        if (req.width > 0 && req.height > 0) {
            long long best_delta = -1;
            for (size_t i = 0; i < modes.size(); ++i) {
                const CameraMode& m = modes[i].mode;
                const long long delta = std::llabs((long long)m.width - req.width) +
                                        std::llabs((long long)m.height - req.height);
                const double fps_delta = (req.fps > 0.0) ? std::fabs(m.fps - req.fps) : -m.fps;
                const double best_fps_delta = (req.fps > 0.0)
                                                  ? std::fabs(modes[best].mode.fps - req.fps)
                                                  : -modes[best].mode.fps;
                if (best_delta < 0 || delta < best_delta ||
                    (delta == best_delta && fps_delta < best_fps_delta)) {
                    best_delta = delta;
                    best = i;
                }
            }
            return best;
        }

        // `modes` is already sorted (pixels desc, fps desc), so index 0 is the max-resolution pick.
        if (req.prefer_max_resolution) {
            if (req.fps <= 0.0) return 0;
            // Honour an fps request within the winning resolution.
            const CameraMode& top = modes[0].mode;
            double best_delta = std::fabs(top.fps - req.fps);
            for (size_t i = 1; i < modes.size(); ++i) {
                const CameraMode& m = modes[i].mode;
                if (m.width != top.width || m.height != top.height) break;
                const double delta = std::fabs(m.fps - req.fps);
                if (delta < best_delta) {
                    best_delta = delta;
                    best = i;
                }
            }
            return best;
        }

        // Framerate-first: closest to the request, or the fastest mode when none was given.
        const double want = req.fps > 0.0 ? req.fps : 1e9;
        for (size_t i = 1; i < modes.size(); ++i)
            if (std::fabs(modes[i].mode.fps - want) < std::fabs(modes[best].mode.fps - want))
                best = i;
        return best;
    }

    // A format renegotiation mid-stream (some UVC drivers do this on the first frames) invalidates
    // our cached geometry; re-read it rather than decoding against stale dimensions.
    bool refreshCurrentType() {
        ComRef<IMFMediaType> current;
        if (FAILED(reader_->GetCurrentMediaType(MF_SOURCE_READER_FIRST_VIDEO_STREAM,
                                                current.put()))) {
            last_error_ = "media type changed mid-stream and could not be re-read";
            return false;
        }
        GUID sub = GUID_NULL;
        current->GetGUID(MF_MT_SUBTYPE, &sub);
        const Decode decode = decodeFor(sub);
        if (decode == Decode::None) {
            last_error_ = "device switched to undecodable format " + describeSubtype(sub).label;
            return false;
        }
        UINT32 w = 0, h = 0;
        if (FAILED(::MFGetAttributeSize(current.get(), MF_MT_FRAME_SIZE, &w, &h)) || !w || !h) {
            last_error_ = "renegotiated media type has no frame size";
            return false;
        }
        UINT32 declared = 0;
        if (SUCCEEDED(current->GetUINT32(MF_MT_DEFAULT_STRIDE, &declared)))
            default_stride_ = (LONG)(INT32)declared;
        else if (FAILED(::MFGetStrideForBitmapInfoHeader(sub.Data1, w, &default_stride_)))
            default_stride_ = (LONG)(w * bytesPerPixel(decode));

        decode_ = decode;
        out_w_ = (int)w;
        out_h_ = (int)h;
        active_.width = (int)w;
        active_.height = (int)h;
        return true;
    }

    // ---- control plumbing ---------------------------------------------------------------------

    // UVC sources expose the legacy DirectShow control interfaces either directly off the media
    // source or behind IMFGetService; try both before declaring a control unsupported.
    void bindControlInterfaces() {
        if (!source_) return;
        if (FAILED(source_->QueryInterface(IID_PPV_ARGS(cam_ctrl_.put())))) {
            cam_ctrl_.reset();
            ComRef<IMFGetService> svc;
            if (SUCCEEDED(source_->QueryInterface(IID_PPV_ARGS(svc.put()))))
                if (FAILED(svc->GetService(GUID_NULL, IID_IAMCameraControl, cam_ctrl_.putVoid())))
                    cam_ctrl_.reset();
        }
        if (FAILED(source_->QueryInterface(IID_PPV_ARGS(proc_amp_.put())))) {
            proc_amp_.reset();
            ComRef<IMFGetService> svc;
            if (SUCCEEDED(source_->QueryInterface(IID_PPV_ARGS(svc.put()))))
                if (FAILED(svc->GetService(GUID_NULL, IID_IAMVideoProcAmp, proc_amp_.putVoid())))
                    proc_amp_.reset();
        }
    }

    // Focus / Exposure / Zoom live on IAMCameraControl; WhiteBalance / Gain / Brightness on
    // IAMVideoProcAmp. A control whose interface or property is absent stays supported=false,
    // which is what makes the UI grey the slider out instead of lying about it.
    static bool isCameraControlProperty(CameraControl c, long& prop) {
        switch (c) {
            case CameraControl::Focus:    prop = CameraControl_Focus;    return true;
            case CameraControl::Exposure: prop = CameraControl_Exposure; return true;
            case CameraControl::Zoom:     prop = CameraControl_Zoom;     return true;
            default: return false;
        }
    }

    static bool isProcAmpProperty(CameraControl c, long& prop) {
        switch (c) {
            case CameraControl::WhiteBalance: prop = VideoProcAmp_WhiteBalance; return true;
            case CameraControl::Gain:         prop = VideoProcAmp_Gain;         return true;
            case CameraControl::Brightness:   prop = VideoProcAmp_Brightness;   return true;
            default: return false;
        }
    }

    bool readControl(CameraControl c, long& min, long& max, long& step, long& def, long& caps,
                     long& value, long& flags) {
        long prop = 0;
        if (isCameraControlProperty(c, prop)) {
            if (!cam_ctrl_) return false;
            if (FAILED(cam_ctrl_->GetRange(prop, &min, &max, &step, &def, &caps))) return false;
            return SUCCEEDED(cam_ctrl_->Get(prop, &value, &flags));
        }
        if (isProcAmpProperty(c, prop)) {
            if (!proc_amp_) return false;
            if (FAILED(proc_amp_->GetRange(prop, &min, &max, &step, &def, &caps))) return false;
            return SUCCEEDED(proc_amp_->Get(prop, &value, &flags));
        }
        return false;
    }

    bool writeControl(CameraControl c, long raw, bool automatic) {
        long prop = 0;
        if (isCameraControlProperty(c, prop)) {
            if (!cam_ctrl_) return false;
            const long f = automatic ? CameraControl_Flags_Auto : CameraControl_Flags_Manual;
            const HRESULT hr = cam_ctrl_->Set(prop, raw, f);
            if (FAILED(hr)) { fail("IAMCameraControl::Set", hr); return false; }
            return true;
        }
        if (isProcAmpProperty(c, prop)) {
            if (!proc_amp_) return false;
            const long f = automatic ? VideoProcAmp_Flags_Auto : VideoProcAmp_Flags_Manual;
            const HRESULT hr = proc_amp_->Set(prop, raw, f);
            if (FAILED(hr)) { fail("IAMVideoProcAmp::Set", hr); return false; }
            return true;
        }
        return false;
    }
};

std::unique_ptr<CameraBackend> createMFCamera() {
    return std::make_unique<MFCameraBackend>();
}

}  // namespace chudware::platform

#endif  // _WIN32
