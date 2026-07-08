// image_gallery.cpp — thumbnail grid with drag-drop + quality badges (RE-19)
// Thumbnail loading reuses reveng::loadTexFromFile (RE-08 img_tex).
// Drag-drop accepts kFileDragPayload from the OS file panel.
// Quality badge colours: green=ok, amber=blurred, red=duplicate.
// GL-thread only.
#include "image_gallery.h"

#include <cstdio>
#include <cstring>
#include <string>
#include <vector>
#include <algorithm>

#include "imgui.h"
#include <nlohmann/json.hpp>

#include "core/app_state.h"
#include "ui_theme.h"
#include "img_tex.h"    // RE-08: loadTexFromFile / freeTexture

namespace ui {
namespace reveng {

// Drag-drop payload type used across the host for file drops.
static constexpr const char* kFileDragPayload = "FILES";

// Thumbnail size in logical pixels.
static constexpr float kThumbSize = 80.0f;
static constexpr int   kMaxThumbLoad = 4;  // load at most N new thumbs per frame

// ---------------------------------------------------------------------------
// Thumbnail loading
// ---------------------------------------------------------------------------

void galleryLoadPendingThumbs(std::vector<GalleryImage>& images) {
    int loaded = 0;
    for (auto& img : images) {
        if (img.texId || img.path.empty() || img.texLoading) continue;
        if (loaded >= kMaxThumbLoad) break;
        // Load synchronously — img_tex uses stb_image, fast for small files.
        // For production, this would move to a background thread, but RE-19 is
        // capture-session scale (tens of images) so sync is fine.
        int w = 0, h = 0;
        const unsigned int tex = ::reveng::loadTexFromFile(img.path, &w, &h);
        img.texId  = tex;
        img.thumbW = w;
        img.thumbH = h;
        ++loaded;
    }
}

void galleryFreeTextures(std::vector<GalleryImage>& images) {
    for (auto& img : images) {
        if (img.texId) { ::reveng::freeTexture(img.texId); img.texId = 0; }
    }
}

// ---------------------------------------------------------------------------
// Quality badge helper
// ---------------------------------------------------------------------------

static ImU32 badgeColor(const std::string& quality) {
    if (quality == "blurred")   return IM_COL32(220, 150,  20, 220);
    if (quality == "duplicate") return IM_COL32(200,  50,  50, 220);
    if (quality == "ok" || quality.empty()) return IM_COL32(40, 180, 60, 220);
    return IM_COL32(120, 120, 120, 200);
}

static const char* badgeLabel(const std::string& quality) {
    if (quality == "blurred")   return "blur";
    if (quality == "duplicate") return "dup";
    return "ok";
}

// ---------------------------------------------------------------------------
// Main draw function
// ---------------------------------------------------------------------------

void drawImageGallery(AppState& st,
                      const std::string& session_id,
                      std::vector<GalleryImage>& images,
                      std::vector<std::string>& dropped_paths) {
    (void)st;  // reserved for future async badge refresh
    const float sc  = ui::uiScale;
    const float tsz = kThumbSize * sc;

    dropped_paths.clear();

    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(8 * sc, 8 * sc));
    ImGui::BeginChild("##img_gallery", ImVec2(0, 0), ImGuiChildFlags_Border);
    ImGui::PopStyleVar();

    // Header
    ImGui::TextUnformatted("Images");
    ImGui::SameLine();
    ImGui::TextDisabled("(%d)", (int)images.size());
    ImGui::Separator();

    // Drag-drop target on the whole gallery area
    if (ImGui::BeginDragDropTarget()) {
        if (const ImGuiPayload* pl = ImGui::AcceptDragDropPayload(kFileDragPayload)) {
            // Payload is a newline-separated list of paths.
            const char* data = static_cast<const char*>(pl->Data);
            const char* p = data;
            while (p && *p) {
                const char* nl = std::strchr(p, '\n');
                const std::string line(p, nl ? nl : p + std::strlen(p));
                if (!line.empty()) dropped_paths.push_back(line);
                if (!nl) break;
                p = nl + 1;
            }
        }
        ImGui::EndDragDropTarget();
    }

    // Thumbnail grid
    const float cellW  = tsz + 4.0f * sc;
    const float availW = ImGui::GetContentRegionAvail().x;
    const int   cols   = std::max(1, (int)(availW / cellW));

    if (images.empty()) {
        ImGui::Dummy(ImVec2(0, 20.0f * sc));
        ImGui::TextDisabled("Drop image files here or use the webcam shutter.");
    }

    for (int i = 0; i < (int)images.size(); ++i) {
        GalleryImage& img = images[i];
        ImGui::PushID(i);

        if (i % cols != 0) ImGui::SameLine(0.0f, 4.0f * sc);

        // Thumbnail or placeholder
        ImGui::BeginGroup();
        if (img.texId) {
            // Preserve aspect ratio
            const float aspect = (img.thumbH > 0)
                                 ? (float)img.thumbW / (float)img.thumbH
                                 : 1.0f;
            const float dh = (aspect >= 1.0f) ? tsz / aspect : tsz;
            const float dw = (aspect >= 1.0f) ? tsz : tsz * aspect;
            const float padX = (tsz - dw) * 0.5f;
            const float padY = (tsz - dh) * 0.5f;
            ImGui::Dummy(ImVec2(padX, padY));
            ImGui::Image((ImTextureID)(uintptr_t)img.texId, ImVec2(dw, dh));
        } else {
            // Placeholder box
            ImDrawList* dl = ImGui::GetWindowDrawList();
            const ImVec2 p0 = ImGui::GetCursorScreenPos();
            dl->AddRectFilled(p0, ImVec2(p0.x + tsz, p0.y + tsz),
                              IM_COL32(50, 50, 60, 200), 4.0f);
            dl->AddRect(p0, ImVec2(p0.x + tsz, p0.y + tsz),
                        IM_COL32(100, 100, 120, 180), 4.0f);
            ImGui::Dummy(ImVec2(tsz, tsz));
        }

        // Quality badge (top-right corner of the cell)
        {
            ImDrawList* dl = ImGui::GetWindowDrawList();
            const ImVec2 cellPos = ImGui::GetItemRectMin();
            const char*  lbl     = badgeLabel(img.quality);
            const ImVec2 lsz     = ImGui::CalcTextSize(lbl);
            const float  bW      = lsz.x + 6.0f * sc;
            const float  bH      = lsz.y + 2.0f * sc;
            const ImVec2 br(cellPos.x + tsz, cellPos.y);
            const ImVec2 bl(br.x - bW, br.y);
            dl->AddRectFilled(bl, ImVec2(br.x, br.y + bH),
                              badgeColor(img.quality), 3.0f);
            dl->AddText(ImVec2(bl.x + 3.0f * sc, bl.y + 1.0f * sc),
                        IM_COL32(255, 255, 255, 230), lbl);
        }

        // Tooltip with path
        if (ImGui::IsItemHovered()) {
            ImGui::SetTooltip("%s", img.path.c_str());
        }
        ImGui::EndGroup();
        ImGui::PopID();
    }

    // Drop-zone hint at the bottom when gallery has content
    if (!images.empty()) {
        ImGui::Spacing();
        ImGui::TextDisabled("+ drop more images here");
    }

    // Handle dropped paths: add GalleryImage entries + fire async RPC if session active
    for (const auto& p : dropped_paths) {
        // Avoid duplicates
        const bool dup = std::any_of(images.begin(), images.end(),
                                     [&p](const GalleryImage& g){ return g.path == p; });
        if (!dup) {
            GalleryImage g;
            g.path    = p;
            g.quality = "";   // unknown until reveng_add_images returns
            images.push_back(std::move(g));
        }
    }
    // Note: caller in reveng_workspace fires the async reveng_add_images RPC with
    // dropped_paths; gallery just owns the visual state.

    ImGui::EndChild();

    // Session label
    if (session_id.empty()) {
        ImGui::TextDisabled("No active session — dropped images will not be sent.");
    }
}

}  // namespace reveng
}  // namespace ui
