// Copilot attachment image chrome: square composer thumbnails, aspect-preserving sent-message
// previews, and the full-size viewer ("lightbox") a click on either opens.
//
// Decode and thumbnail resize happen on one background worker. The shared bounded cache uploads
// at most one image per GUI frame; the viewer requests a full-resolution variant separately.
//
// The tile deliberately does NOT open a nested Begin()/End() window: a nested window over a
// widget breaks hit-testing in this codebase (a real, previously-shipped bug — see
// ui/copilot/composer.cpp's overlay chrome, which all paints into the PARENT window draw list).
// Everything here paints into the current window's draw list and reserves its area with a single
// InvisibleButton, so hover/click are exact.

#include "ui/copilot/image_lightbox.h"

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <string>
#include <unordered_map>
#include <condition_variable>
#include <deque>
#include <filesystem>
#include <memory>
#include <mutex>
#include <thread>
#include <vector>
#include "stb_image.h"
#include "stb_image_resize2.h"
#include "ui/anim.h"

#include "imgui.h"
#include "imgui_internal.h"      // ImRect (adaptivePanel/glassPanel) + GetKeyData (Esc swallow)

#include "core/app_state.h"      // ::Tex / AppState (fonts)
#include "ui_theme.h"            // ui::palette / uiScale / adaptivePanel / glassPanel
#include "ui/metrics.h"          // px / rounding / stroke — DPI-aware sizes
#include "ui/clipboard_image.h"  // ui::isImageAttachment — is this attachment a raster image?
#include "gl_compat.h"           // glDeleteTextures — LRU eviction of thumbnail textures

namespace ui {
namespace {

struct ThumbTex {
    ::Tex tex;
    uint64_t use = 0;
    int frame = -1;
    bool pending = false, known = false;
    std::filesystem::file_time_type mtime{};
    double nextCheck = 0;
};
struct ImageJob {
    std::weak_ptr<ThumbTex> slot;
    std::string path;
    int edge = 0;
    bool known = false;
    std::filesystem::file_time_type mtime{};
};
struct ImageResult {
    std::weak_ptr<ThumbTex> slot;
    std::vector<unsigned char> pixels;
    std::unique_ptr<unsigned char, void(*)(void*)> original{nullptr, stbi_image_free};
    int w = 0, h = 0;
    bool unchanged = false;
    std::filesystem::file_time_type mtime{};
};
struct ImageCache {
    std::unordered_map<std::string, std::shared_ptr<ThumbTex>> slots;
    uint64_t clock = 0;
    int uploadedFrame = -1;
    size_t textureBytes = 0;
    std::mutex mutex;
    std::condition_variable cv;
    std::deque<ImageJob> jobs;
    std::deque<ImageResult> ready;
    bool stop = false;
    std::thread worker;

    ~ImageCache() {
        { std::lock_guard<std::mutex> lock(mutex); stop = true; }
        cv.notify_one();
        if (worker.joinable()) worker.join();
    }
    void run() {
        for (;;) {
            ImageJob job;
            {
                std::unique_lock<std::mutex> lock(mutex);
                cv.wait(lock, [&] { return stop || (!jobs.empty() && ready.size() < 2); });
                if (stop) return;
                job = std::move(jobs.front());
                jobs.pop_front();
            }
            if (job.slot.expired()) continue;
            ImageResult result;
            result.slot = job.slot;
            std::error_code ec;
            result.mtime = std::filesystem::last_write_time(job.path, ec);
            result.unchanged = job.known && result.mtime == job.mtime;
            if (!result.unchanged && !ec) {
                int w = 0, h = 0, channels = 0;
                unsigned char* pixels = stbi_load(job.path.c_str(), &w, &h, &channels, 4);
                if (pixels && w > 0 && h > 0) {
                    const double scale = std::min(1.0, (double)job.edge / std::max(w, h));
                    result.w = std::max(1, (int)(w * scale));
                    result.h = std::max(1, (int)(h * scale));
                    if (result.w == w && result.h == h) {
                        result.original.reset(pixels);
                        pixels = nullptr;
                    } else {
                        result.pixels.resize((size_t)result.w * result.h * 4);
                        if (!stbir_resize_uint8_srgb(pixels, w, h, 0, result.pixels.data(),
                                                    result.w, result.h, 0, STBIR_RGBA)) {
                            result.pixels.clear();
                            result.w = result.h = 0;
                        }
                    }
                }
                stbi_image_free(pixels);
            }
            {
                std::lock_guard<std::mutex> lock(mutex);
                if (stop) return;
                ready.push_back(std::move(result));
            }
            ui::anim::wakeLoop();
        }
    }
    bool evict(int frame) {
        auto oldest = slots.end();
        for (auto it = slots.begin(); it != slots.end(); ++it)
            if (it->second->frame != frame &&
                (oldest == slots.end() || it->second->use < oldest->second->use))
                oldest = it;
        if (oldest == slots.end()) return false;
        const ::Tex& tex = oldest->second->tex;
        if (tex.id) {
            const GLuint id = tex.id;
            glDeleteTextures(1, &id);
            textureBytes -= (size_t)tex.w * tex.h * 4;
        }
        slots.erase(oldest);
        return true;
    }
    void upload(int frame) {
        if (uploadedFrame == frame) return;
        ImageResult result;
        {
            std::lock_guard<std::mutex> lock(mutex);
            if (ready.empty()) return;
            result = std::move(ready.front());
            ready.pop_front();
        }
        cv.notify_one();
        auto slot = result.slot.lock();
        if (!slot) return;
        slot->pending = false;
        slot->known = true;
        slot->mtime = result.mtime;
        if (result.unchanged) return;
        uploadedFrame = frame;
        // Never delete a texture already referenced by this frame's ImDrawData.
        if (slot->frame == frame) {
            slot->pending = true;
            std::lock_guard<std::mutex> lock(mutex);
            ready.push_front(std::move(result));
            ui::anim::requestFramesAt(30);
            return;
        }
        if (slot->tex.id) {
            const GLuint old = slot->tex.id;
            glDeleteTextures(1, &old);
            textureBytes -= (size_t)slot->tex.w * slot->tex.h * 4;
            slot->tex = {};
        }
        if (result.pixels.empty() && !result.original) return;
        const size_t bytes = (size_t)result.w * result.h * 4;
        const size_t limit = std::max(size_t(256 * 1024 * 1024), bytes);
        slot->frame = frame;
        while (textureBytes + bytes > limit && evict(frame)) {}
        if (textureBytes + bytes > limit) {
            slot->pending = true;
            std::lock_guard<std::mutex> lock(mutex);
            ready.push_front(std::move(result));
            ui::anim::requestFramesAt(30);
            return;
        }
        GLuint id = 0;
        GLint previous = 0;
        glGetIntegerv(GL_TEXTURE_BINDING_2D, &previous);
        GLint unpackBuffer = 0, alignment = 0, rowLength = 0, skipRows = 0, skipPixels = 0;
        glGetIntegerv(GL_PIXEL_UNPACK_BUFFER_BINDING, &unpackBuffer);
        glGetIntegerv(GL_UNPACK_ALIGNMENT, &alignment);
        glGetIntegerv(GL_UNPACK_ROW_LENGTH, &rowLength);
        glGetIntegerv(GL_UNPACK_SKIP_ROWS, &skipRows);
        glGetIntegerv(GL_UNPACK_SKIP_PIXELS, &skipPixels);
        glBindBuffer(GL_PIXEL_UNPACK_BUFFER, 0);
        glPixelStorei(GL_UNPACK_ALIGNMENT, 1);
        glPixelStorei(GL_UNPACK_ROW_LENGTH, 0);
        glPixelStorei(GL_UNPACK_SKIP_ROWS, 0);
        glPixelStorei(GL_UNPACK_SKIP_PIXELS, 0);
        glGenTextures(1, &id);
        glBindTexture(GL_TEXTURE_2D, id);
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR);
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
        glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA8, result.w, result.h, 0, GL_RGBA,
                     GL_UNSIGNED_BYTE, result.original ? result.original.get() : result.pixels.data());
        glBindTexture(GL_TEXTURE_2D, (GLuint)previous);
        glBindBuffer(GL_PIXEL_UNPACK_BUFFER, (GLuint)unpackBuffer);
        glPixelStorei(GL_UNPACK_ALIGNMENT, alignment);
        glPixelStorei(GL_UNPACK_ROW_LENGTH, rowLength);
        glPixelStorei(GL_UNPACK_SKIP_ROWS, skipRows);
        glPixelStorei(GL_UNPACK_SKIP_PIXELS, skipPixels);
        slot->tex = {id, result.w, result.h};
        textureBytes += bytes;
    }
};

ImageCache& imageCache() { static ImageCache cache; return cache; }

// A usable upload: non-zero id AND real dimensions (the aspect math divides by them).
bool usable(const ::Tex& t) { return t.id != 0 && t.w > 0 && t.h > 0; }

std::string baseName(const std::string& p) {
    const size_t s = p.find_last_of("/\\");
    return s == std::string::npos ? p : p.substr(s + 1);
}

// Aspect-preserving fit of a texW x texH image inside [b0,b1], centred (letterboxed/pillarboxed).
// `allowUpscale` false clamps the scale at 1:1 so a 64x64 paste is never blown up into a blurry
// wall in the viewer; the tile passes true because a chip-sized tile is a thumbnail by definition.
void fitRect(const ImVec2& b0, const ImVec2& b1, int texW, int texH, bool allowUpscale,
             ImVec2& f0, ImVec2& f1) {
    const float bw = b1.x - b0.x, bh = b1.y - b0.y;
    float k = std::min(bw / (float)texW, bh / (float)texH);
    if (!allowUpscale) k = std::min(k, 1.0f);
    const float dw = (float)texW * k, dh = (float)texH * k;
    f0 = ImVec2(b0.x + (bw - dw) * 0.5f, b0.y + (bh - dh) * 0.5f);
    f1 = ImVec2(f0.x + dw, f0.y + dh);
}

// Dim generic "document" mark, drawn from primitives rather than an atlas cell: an unknown icon
// name would log a warning and paint the magenta placeholder (ui/icons.cpp), and this glyph must
// stay silent — it is what a failed decode falls back to.
void drawFileGlyph(ImDrawList* dl, const ImVec2& c, float side, ImU32 col, float thick) {
    const float w = side * 0.72f, h = side;
    const float fold = side * 0.28f;
    const ImVec2 p0(c.x - w * 0.5f, c.y - h * 0.5f);
    const ImVec2 p1(c.x + w * 0.5f, c.y + h * 0.5f);
    dl->PathLineTo(p0);
    dl->PathLineTo(ImVec2(p1.x - fold, p0.y));
    dl->PathLineTo(ImVec2(p1.x, p0.y + fold));
    dl->PathLineTo(ImVec2(p1.x, p1.y));
    dl->PathLineTo(ImVec2(p0.x, p1.y));
    dl->PathStroke(col, ImDrawFlags_Closed, thick);
    // Folded corner + two content rules.
    dl->AddLine(ImVec2(p1.x - fold, p0.y), ImVec2(p1.x - fold, p0.y + fold), col, thick);
    dl->AddLine(ImVec2(p1.x - fold, p0.y + fold), ImVec2(p1.x, p0.y + fold), col, thick);
    for (int i = 1; i <= 2; ++i) {
        const float y = p0.y + h * (0.42f + 0.20f * (float)i);
        dl->AddLine(ImVec2(p0.x + w * 0.18f, y), ImVec2(p1.x - w * 0.18f, y), col, thick);
    }
}

ImU32 u32(const ImVec4& c) { return ImGui::ColorConvertFloat4ToU32(c); }
ImU32 u32a(const float rgba[4], float a) {
    return u32(ImVec4(rgba[0], rgba[1], rgba[2], a));
}

// Clip `s` to `maxW`, appending a UTF-8 ellipsis when it must be shortened (same idiom as
// docs_panel.cpp's ellipsize, with the atlas-safe glyph the copilot chips already use).
std::string clipText(const std::string& s, float maxW) {
    if (ImGui::CalcTextSize(s.c_str()).x <= maxW) return s;
    std::string out = s;
    while (out.size() > 1 &&
           ImGui::CalcTextSize((out + "\xe2\x80\xa6").c_str()).x > maxW)
        out.pop_back();
    return out + "\xe2\x80\xa6";
}

// ---- viewer state -----------------------------------------------------------------------------
// openImageLightbox is callable from inside another widget's draw/callback, so it ONLY records
// the request; every ImGui call happens in drawImageLightbox.
std::string g_lbPath;
bool        g_lbOpen = false;

}  // namespace

namespace copilot {
void initImageLoader() {
    ImageCache& cache = imageCache();
    if (!cache.worker.joinable())
        cache.worker = std::thread([&cache] { cache.run(); });
}

ImTextureID asyncImageTexture(const std::string& path, int maxEdge, int* outW, int* outH) {
    if (outW) *outW = 0;
    if (outH) *outH = 0;
    if (path.empty()) return 0;
    static const int maxTexture = [] {
        GLint value = 0;
        glGetIntegerv(GL_MAX_TEXTURE_SIZE, &value);
        return std::max(1, (int)value);
    }();
    maxEdge = std::clamp(maxEdge, 1, maxTexture);
    ImageCache& cache = imageCache();
    // Startup owns thread creation; requesting an image must never exhaust the browser pool.
    if (!cache.worker.joinable()) return 0;
    const int frame = ImGui::GetFrameCount();
    cache.upload(frame);
    const std::string key = path + '\x1f' + std::to_string(maxEdge);
    auto it = cache.slots.find(key);
    if (it == cache.slots.end()) {
        while (cache.slots.size() >= 96)
            if (!cache.evict(frame)) { ui::anim::requestFramesAt(30); return 0; }
        it = cache.slots.emplace(key, std::make_shared<ThumbTex>()).first;
    }
    const auto& slot = it->second;
    slot->use = ++cache.clock;
    slot->frame = frame;
    const double now = ImGui::GetTime();
    if (!slot->pending && now >= slot->nextCheck) {
        slot->pending = true;
        slot->nextCheck = now + 2.0 + (std::hash<std::string>{}(key) % 1000) * 0.001;
        {
            std::lock_guard<std::mutex> lock(cache.mutex);
            cache.jobs.erase(std::remove_if(cache.jobs.begin(), cache.jobs.end(),
                [](const ImageJob& job) { return job.slot.expired(); }), cache.jobs.end());
            cache.jobs.push_back({slot, path, maxEdge, slot->known, slot->mtime});
        }
        cache.cv.notify_one();
    }
    if (slot->pending) ui::anim::requestFramesAt(30);
    if (outW) *outW = slot->tex.w;
    if (outH) *outH = slot->tex.h;
    return (ImTextureID)(intptr_t)slot->tex.id;
}
}  // namespace copilot

// =================================================================================================
// thumbnail tile
// =================================================================================================

static bool attachmentTile(const std::string& path, const ImVec2& size, const ::Tex* tex) {
    const auto& pal = palette;
    ImDrawList* dl  = ImGui::GetWindowDrawList();

    const ImVec2 p0 = ImGui::GetCursorScreenPos();
    const ImVec2 p1(p0.x + size.x, p0.y + size.y);

    // Exact hit-test: one InvisibleButton the size of the tile, id derived from the path so a row
    // of chips never collides even when the caller forgot to PushID.
    ImGui::PushID(path.c_str());
    ImGui::InvisibleButton("##attach_thumb", size);
    ImGui::PopID();
    const bool hovered = ImGui::IsItemHovered();
    const bool clicked = ImGui::IsItemClicked(ImGuiMouseButton_Left);

    const float rnd    = metrics::rounding(6.0f);   // matches the composer's attachment chips
    const float thick  = metrics::stroke(1.0f);
    const ImU32 border = Palette::toU32(pal.glassBorder);

    // Backdrop: the letterbox bars and the placeholder share it, so a portrait paste reads as an
    // image inside a chip instead of a floating sliver.
    dl->AddRectFilled(p0, p1, u32a(pal.glassBg, 0.55f), rnd);

    if (tex) {
        ImVec2 f0, f1;
        fitRect(ImVec2(p0.x + thick, p0.y + thick), ImVec2(p1.x - thick, p1.y - thick),
                tex->w, tex->h, /*allowUpscale=*/true, f0, f1);
        // Rounded so a full-bleed landscape image cannot poke out past the tile's corners.
        dl->AddImageRounded((ImTextureID)(intptr_t)tex->id, f0, f1, ImVec2(0, 0), ImVec2(1, 1),
                            IM_COL32_WHITE, rnd);
    } else {
        drawFileGlyph(dl, ImVec2((p0.x + p1.x) * 0.5f, (p0.y + p1.y) * 0.5f),
                      std::min(size.x, size.y) * 0.52f,
                      u32a(pal.textDim, 0.55f), metrics::stroke(1.2f));
    }

    // Reads as clickable: accent wash + accent ring on hover, over the always-present frame.
    dl->AddRect(p0, p1, border, rnd, 0, thick);
    if (hovered) {
        dl->AddRectFilled(p0, p1, u32a(pal.accent, 0.16f), rnd);
        dl->AddRect(p0, p1, u32a(pal.accent, 0.85f), rnd, 0, metrics::stroke(1.5f));
        ImGui::SetMouseCursor(ImGuiMouseCursor_Hand);
        if (tex)
            ImGui::SetTooltip("%s\n%d\xc3\x97%d px \xc2\xb7 click to view full size",
                              baseName(path).c_str(), tex->w, tex->h);
        else
            ImGui::SetTooltip("%s\nNo preview available", baseName(path).c_str());
    }

    return clicked;
}

bool attachmentThumbnail(const std::string& path, float sizePx) {
    if (path.empty() || sizePx < 4.0f) return false;
    ::Tex image;
    const ::Tex* tex = nullptr;
    if (isImageAttachment(path)) {
        image.id = (unsigned int)(intptr_t)copilot::asyncImageTexture(path, 256, &image.w, &image.h);
        if (usable(image)) tex = &image;
    }
    return attachmentTile(path, ImVec2(sizePx, sizePx), tex);
}

bool attachmentImagePreview(const std::string& path, float maxWidth, float maxHeight) {
    if (path.empty() || maxWidth < 4.0f || maxHeight < 4.0f) return false;
    ::Tex image;
    const ::Tex* tex = nullptr;
    if (isImageAttachment(path)) {
        image.id = (unsigned int)(intptr_t)copilot::asyncImageTexture(path, 1024, &image.w, &image.h);
        if (usable(image)) tex = &image;
    }
    ImVec2 size(maxWidth, std::min(maxHeight, ImGui::GetTextLineHeight() * 4.0f));
    if (tex) {
        // Keep small pastes at native resolution; large portrait/landscape images fit both axes.
        const float border = metrics::stroke(1.0f) * 2.0f;
        const float scale = std::min({(maxWidth - border) / static_cast<float>(tex->w),
                                      (maxHeight - border) / static_cast<float>(tex->h), 1.0f});
        size = ImVec2(tex->w * scale + border, tex->h * scale + border);
    }
    return attachmentTile(path, size, tex);
}

// =================================================================================================
// full-size viewer
// =================================================================================================

void openImageLightbox(const std::string& path) {
    if (path.empty()) return;
    g_lbPath = path;
    g_lbOpen = true;
}

void drawImageLightbox(AppState& st) {
    // Nothing open -> not a single window, item or draw call.
    if (!g_lbOpen) return;
    if (g_lbPath.empty()) { g_lbOpen = false; return; }

    ImGuiViewport* vp = ImGui::GetMainViewport();
    const ImVec2 wpos = vp->WorkPos;
    const ImVec2 wsz  = vp->WorkSize;
    if (wsz.x < 32.0f || wsz.y < 32.0f) return;   // degenerate frame (minimised): try again later

    const auto& pal = palette;

    ImGui::SetNextWindowPos(wpos);
    ImGui::SetNextWindowSize(wsz);
    ImGui::SetNextWindowFocus();
    // Liquid-glass v2 recipe: transparent native fill, the scrim + caption glass below are the
    // only paint. Style pushed before Begin (the frame chrome renders during Begin) and popped
    // right after, matching evaluate_dialogs.cpp / cutlist_panel.cpp.
    ImGui::PushStyleColor(ImGuiCol_WindowBg, ImVec4(0, 0, 0, 0));
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(0, 0));
    ImGui::PushStyleVar(ImGuiStyleVar_WindowRounding, 0.0f);
    ImGui::PushStyleVar(ImGuiStyleVar_WindowBorderSize, 0.0f);
    const bool begun = ImGui::Begin("##copilot_image_lightbox", nullptr,
                                    ImGuiWindowFlags_NoDecoration |
                                    ImGuiWindowFlags_NoMove |
                                    ImGuiWindowFlags_NoSavedSettings |
                                    ImGuiWindowFlags_NoDocking |
                                    ImGuiWindowFlags_NoScrollWithMouse);
    ImGui::PopStyleVar(3);
    ImGui::PopStyleColor();

    bool close = false;
    if (begun) {
        ImDrawList* dl = ImGui::GetWindowDrawList();
        const ImVec2 sEnd(wpos.x + wsz.x, wpos.y + wsz.y);
        // ~55% dim: the app's drop-shadow role, so the scrim is the same black everywhere.
        const float* sh = ui::palette.glassShadow;
        dl->AddRectFilled(wpos, sEnd,
                          ImGui::ColorConvertFloat4ToU32(
                              ImVec4(sh[0], sh[1], sh[2], 140.0f / 255.0f)));

        ::Tex tex;
        tex.id = (unsigned int)(intptr_t)copilot::asyncImageTexture(
            g_lbPath, 1 << 30, &tex.w, &tex.h);
        const bool haveT = usable(tex);

        const float pad  = metrics::px(10.0f);
        const float gap  = metrics::px(8.0f);
        const float capH = ImGui::GetFrameHeight() + pad;
        const float boxW = wsz.x * 0.9f;
        const float boxH = std::max(metrics::px(64.0f), wsz.y * 0.9f - capH - gap);

        // Fit inside 90% of the work area, aspect preserved, never upscaled past 1:1.
        float dw, dh;
        if (haveT) {
            ImVec2 f0, f1;
            fitRect(ImVec2(0, 0), ImVec2(boxW, boxH), tex.w, tex.h, /*allowUpscale=*/false, f0, f1);
            dw = f1.x - f0.x;
            dh = f1.y - f0.y;
        } else {
            dw = dh = std::min(metrics::px(180.0f), std::min(boxW, boxH));
        }

        // Caption strip above the image; the pair is centred in the work area as one group.
        const float capW  = std::max(dw, std::min(boxW, metrics::px(340.0f)));
        const float total = capH + gap + dh;
        const float top   = wpos.y + std::max(0.0f, (wsz.y - total) * 0.5f);
        const ImVec2 cap0(wpos.x + (wsz.x - capW) * 0.5f, top);
        const ImVec2 cap1(cap0.x + capW, top + capH);
        const ImVec2 img0(wpos.x + (wsz.x - dw) * 0.5f, cap1.y + gap);
        const ImVec2 img1(img0.x + dw, img0.y + dh);

        const AdaptiveSurface& surf =
            adaptivePanel(panelId("copilot_image_lightbox"), ImRect(cap0, cap1),
                          GlassGroup::Global);
        glassPanel(dl, ImRect(cap0, cap1), GlassLevel::Regular, 8.0f, surf);

        // ---- the image itself ----
        const float thick  = metrics::stroke(1.0f);
        const ImU32 border = Palette::toU32(pal.glassBorder);
        if (haveT) {
            dl->AddImage((ImTextureID)(intptr_t)tex.id, img0, img1);
            dl->AddRect(img0, img1, border, 0.0f, 0, thick);
        } else {
            const float rnd = metrics::rounding(8.0f);
            dl->AddRectFilled(img0, img1, u32a(pal.glassBg, 0.75f), rnd);
            dl->AddRect(img0, img1, border, rnd, 0, thick);
            drawFileGlyph(dl, ImVec2((img0.x + img1.x) * 0.5f, (img0.y + img1.y) * 0.5f),
                          dh * 0.46f, u32a(pal.textDim, 0.6f), metrics::stroke(1.4f));
        }

        // ---- caption strip: name (clipped) + dimensions + Esc hint + close button ----
        const float btn = ImGui::GetFrameHeight();
        char dims[64] = {};
        if (haveT) std::snprintf(dims, sizeof(dims), "%d\xc3\x97%d px", tex.w, tex.h);
        std::string meta = dims[0] ? std::string(dims) + "  \xc2\xb7  Esc closes" : "Esc closes";

        const float metaW   = ImGui::CalcTextSize(meta.c_str()).x;
        const float textY   = cap0.y + (capH - ImGui::GetTextLineHeight()) * 0.5f;
        const float nameEnd = cap1.x - pad * 0.5f - btn - pad - metaW - pad;
        const std::string name =
            clipText(baseName(g_lbPath), std::max(metrics::px(40.0f), nameEnd - (cap0.x + pad)));

        ImFont* nameFont = st.fonts.medium ? st.fonts.medium : ImGui::GetFont();
        ImGui::SetCursorScreenPos(ImVec2(cap0.x + pad, textY));
        ImGui::PushFont(nameFont);
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textPrimary);
        ImGui::TextUnformatted(name.c_str());
        ImGui::PopStyleColor();
        ImGui::PopFont();

        ImGui::SetCursorScreenPos(ImVec2(cap1.x - pad * 0.5f - btn - pad - metaW, textY));
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textSecondary);
        ImGui::TextUnformatted(meta.c_str());
        ImGui::PopStyleColor();

        ImGui::SetCursorScreenPos(ImVec2(cap1.x - pad * 0.5f - btn, cap0.y + pad * 0.5f));
        ImGui::PushStyleVar(ImGuiStyleVar_FrameRounding, metrics::rounding(4.0f));
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textPrimary);
        if (ImGui::Button("\xc3\x97", ImVec2(btn, btn))) close = true;   // ×
        ImGui::PopStyleColor();
        ImGui::PopStyleVar();
        if (ImGui::IsItemHovered()) ImGui::SetTooltip("Close (Esc)");

        // ---- click on the scrim OUTSIDE the image closes; inside must NOT ----
        // Resolved from the mouse position instead of a full-viewport InvisibleButton: an
        // InvisibleButton covering the work area still reports the press for a click that landed
        // on the close button drawn over it (the earlier item's press is decided at submission
        // time), which would double-handle every close.
        const ImVec2 mp = ImGui::GetMousePos();
        const bool inImg = mp.x >= img0.x && mp.x <= img1.x && mp.y >= img0.y && mp.y <= img1.y;
        const bool inCap = mp.x >= cap0.x && mp.x <= cap1.x && mp.y >= cap0.y && mp.y <= cap1.y;
        if (!inImg && !inCap && !ImGui::IsAnyItemHovered() &&
            ImGui::IsWindowHovered(ImGuiHoveredFlags_ChildWindows) &&
            ImGui::IsMouseClicked(ImGuiMouseButton_Left))
            close = true;

        // ---- Esc closes, and is swallowed ----
        // Handlers behind the viewer (viewport tool/camera Esc paths, the feature tree's
        // selection clear, the sketcher's cancelTool) all call the PUBLIC IsKeyPressed, which
        // ignores ImGui's key-owner system — so ownership cannot hide the key from them.
        // Pushing DownDuration off zero is what makes a later IsKeyPressed(key, false) read
        // false, the cross-TU equivalent of sketch_mode.cpp's local `escHandled` latch. Sites
        // that ran EARLIER this frame (the copilot composer's Esc chain) are no-ops unless the
        // slash menu / staged edit / context pick is armed, so nothing behind reacts in practice.
        if (ImGui::IsKeyPressed(ImGuiKey_Escape, false)) {
            close = true;
            if (ImGuiKeyData* kd = ImGui::GetKeyData(ImGuiKey_Escape)) kd->DownDuration = 0.001f;
        }
    }
    ImGui::End();

    if (close) {
        g_lbOpen = false;
        g_lbPath.clear();
    }
}

}  // namespace ui
