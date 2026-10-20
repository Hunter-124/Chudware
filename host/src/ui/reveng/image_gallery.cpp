// image_gallery.cpp — reverse-engineering capture contact sheet (RE-19; overhauled RE-40)
//
// The capture set is the single input photogrammetry has. If the user cannot SEE which frames are
// sharp, which are duplicates and which never came from a real camera, they cannot cull — and an
// uncullable capture set is what produces a garbage reconstruction three stages later. So this
// panel is a real contact sheet: responsive tile grid, hover magnifier with metadata, full-size
// viewer with next/prev, honest badges (an unrated frame gets NO badge rather than a reassuring
// green dot), a filter + count summary, and per-frame cull / reveal actions.
//
// Thumbnails come from reveng::cachedThumbTex — a bounded, downscaled, LRU-cached decode. The only
// full-resolution decode in this file is the one the full-size viewer asks for, and it is freed
// the moment the viewer moves on.
//
// GL-thread only. No sync worker calls; this panel issues no RPC at all — it reports intents
// through GalleryActions and the Sources panel owns the async half.
#include "image_gallery.h"

#include <algorithm>
#include <cstdio>
#include <cstring>
#include <string>
#include <vector>

#include "imgui.h"
#include "imgui_internal.h"   // ImRect / BeginDragDropTargetCustom (window-wide drop zone)

#include "core/app_state.h"
#include "ui_theme.h"
#include "ui/metrics.h"
#include "reveng_widgets.h"    // the RE visual kit — every pixel in here comes from it
#include "reveng_internal.h"   // RevEngState::selectedImage (read/written; file owned elsewhere)
#include "img_tex.h"           // cachedThumbTex / loadTexFromFile / freeTexture
#include "../file_dialog.h"    // openFileDialog — empty-state browse affordance

namespace ui {
namespace reveng {
namespace {

// Drag-drop payload type used across the host for file drops.
constexpr const char* kFileDragPayload = "FILES";

// Popup id for the full-size viewer.
constexpr const char* kViewerPopup = "Photo##re_gal_viewer";

// At most N NEW decodes per frame. Cache hits are free and unbounded, so re-entering a session
// repopulates the whole sheet in one frame while a fresh 200-photo drop fills in progressively
// instead of stalling the frame that received the drop.
constexpr int kMaxThumbDecodesPerFrame = 4;

// Upper bound on how far the shared thumb cache may grow to hold one capture set resident. At a
// 196-px bounded edge an RGBA thumbnail is ~150 KB, so this ceiling is ~58 MB of VRAM; past it the
// LRU churns rather than the app eating memory without limit.
constexpr int kThumbCacheHardMax = 384;

// ---- filter lanes ---------------------------------------------------------------------------

enum Filter { FilterAll = 0, FilterSharp, FilterBlurred, FilterDuplicate, FilterCount };
const char* const kFilterLabels[FilterCount] = { "All", "Sharp", "Blurred", "Duplicates" };

// ---- panel-local state ----------------------------------------------------------------------
// Panel-local by policy: only the shell agent edits reveng_internal.h, and none of this is shared
// with another stage. The one piece of genuinely shared state — which tile is selected — lives in
// RevEngState::selectedImage because the Scale stage's two-point picker reads it.

struct GalleryUI {
    int  filter   = FilterAll;
    bool cardOpen = true;

    // Reused per-frame index buffer for the filtered view (no per-frame allocation).
    std::vector<int> shown;

    // Hover bookkeeping. The action strip is drawn AFTER the grid loop so it cannot corrupt the
    // row layout (SameLine anchors on the previously submitted item), which means "is the pointer
    // over the strip?" is only known one frame late — good enough to stop tooltip/strip fighting.
    bool prevOverActions = false;

    // Last observed thumb-cache eviction epoch (see galleryLoadPendingThumbs).
    unsigned long long thumbEpoch = 0;

    // Full-size viewer. Holds its OWN full-resolution texture, deliberately outside the thumb
    // cache: one 48 MB entry would evict the entire contact sheet.
    bool         viewerOpen    = false;
    bool         viewerRequest = false;   // one-shot: open the popup at parent-window scope
    int          viewerIdx     = -1;
    std::string  viewerPath;              // path the loaded texture belongs to
    unsigned int viewerTex     = 0;
    int          viewerW = 0, viewerH = 0;
    bool         viewerFailed  = false;
};

GalleryUI& gui() { static GalleryUI g; return g; }

// ---- small helpers --------------------------------------------------------------------------

std::string baseName(const std::string& p) {
    const size_t s = p.find_last_of("/\\");
    return (s == std::string::npos) ? p : p.substr(s + 1);
}

// The tile badge. `synthetic` outranks everything: a generated frame is dimensionally worthless
// and must never be mistaken for a photograph. An unscored frame returns "" — no badge at all,
// because painting it green was the original dead-UI lie this overhaul exists to remove.
const char* badgeFor(const GalleryImage& img) {
    if (img.synthetic || img.quality == "synthetic") return "synthetic";
    if (img.quality == "blurred")   return "blurred";
    if (img.quality == "duplicate") return "duplicate";
    if (img.quality == "ok")        return "ok";
    return "";
}

const char* qualityVerdict(const GalleryImage& img) {
    if (img.texFailed) return "unreadable - the file could not be decoded";
    const char* b = badgeFor(img);
    if (std::strcmp(b, "synthetic") == 0) return "synthetic - not a real photo; no usable dimensions";
    if (std::strcmp(b, "blurred")   == 0) return "blurred - cull it or re-shoot that angle";
    if (std::strcmp(b, "duplicate") == 0) return "duplicate - adds cost, no new parallax";
    if (std::strcmp(b, "ok")        == 0) return "sharp - kept";
    return "unrated - not scored yet";
}

kit::Level levelFor(const GalleryImage& img) {
    if (img.texFailed) return kit::Level::Error;
    const char* b = badgeFor(img);
    if (std::strcmp(b, "synthetic") == 0) return kit::Level::Error;
    if (std::strcmp(b, "blurred")   == 0) return kit::Level::Warn;
    if (std::strcmp(b, "duplicate") == 0) return kit::Level::Warn;
    if (std::strcmp(b, "ok")        == 0) return kit::Level::Ok;
    return kit::Level::Neutral;
}

bool passesFilter(const GalleryImage& img, int filter) {
    const char* b = badgeFor(img);
    switch (filter) {
        case FilterSharp:     return std::strcmp(b, "ok")        == 0;
        case FilterBlurred:   return std::strcmp(b, "blurred")   == 0;
        case FilterDuplicate: return std::strcmp(b, "duplicate") == 0;
        default:              return true;
    }
}

struct Counts {
    int total = 0, ok = 0, blurred = 0, dup = 0, synth = 0, unrated = 0, unreadable = 0;
};

Counts tally(const std::vector<GalleryImage>& v) {
    Counts c;
    c.total = static_cast<int>(v.size());
    for (const auto& img : v) {
        if (img.texFailed) ++c.unreadable;
        const char* b = badgeFor(img);
        if      (std::strcmp(b, "synthetic") == 0) ++c.synth;
        else if (std::strcmp(b, "blurred")   == 0) ++c.blurred;
        else if (std::strcmp(b, "duplicate") == 0) ++c.dup;
        else if (std::strcmp(b, "ok")        == 0) ++c.ok;
        else                                       ++c.unrated;
    }
    return c;
}

// "32 images · 4 blurred · 1 duplicate · 2 unrated"
std::string summaryText(const Counts& c) {
    char buf[96];
    std::snprintf(buf, sizeof buf, "%d image%s", c.total, c.total == 1 ? "" : "s");
    std::string s(buf);
    auto add = [&](int n, const char* word) {
        if (n <= 0) return;
        std::snprintf(buf, sizeof buf, " \xc2\xb7 %d %s", n, word);
        s += buf;
    };
    add(c.blurred,    "blurred");
    add(c.dup,        "duplicate");
    add(c.synth,      "synthetic");
    add(c.unreadable, "unreadable");
    add(c.unrated,    "unrated");
    return s;
}

std::vector<std::string> metaLines(const GalleryImage& img, int idx, int total) {
    std::vector<std::string> m;
    char buf[192];

    m.push_back(baseName(img.path));

    if (img.srcW > 0 && img.srcH > 0) {
        std::snprintf(buf, sizeof buf, "%d x %d px", img.srcW, img.srcH);
        m.emplace_back(buf);
    } else {
        m.emplace_back("pixel size unknown");
    }

    if (img.sharpness >= 0.0) {
        std::snprintf(buf, sizeof buf, "sharpness %.1f", img.sharpness);
        m.emplace_back(buf);
    } else {
        m.emplace_back("sharpness not scored");
    }

    m.emplace_back(qualityVerdict(img));

    std::snprintf(buf, sizeof buf, "#%d of %d", idx + 1, total);
    m.emplace_back(buf);
    return m;
}

// Append `path` as an unrated entry unless it is already in the set. Returns true when added.
bool appendUnique(std::vector<GalleryImage>& images, const std::string& path) {
    if (path.empty()) return false;
    const bool dup = std::any_of(images.begin(), images.end(),
                                 [&path](const GalleryImage& g) { return g.path == path; });
    if (dup) return false;
    GalleryImage g;
    g.path = path;
    g.quality.clear();   // unrated until reveng_add_images / get_reveng_coverage scores it
    images.push_back(std::move(g));
    return true;
}

// ---- full-size viewer -------------------------------------------------------------------------

void viewerRelease(GalleryUI& g) {
    // viewerTex came from loadTexFromFile and is NOT cache-owned, so this is a real delete.
    if (g.viewerTex) { ::reveng::freeTexture(g.viewerTex); g.viewerTex = 0; }
    g.viewerPath.clear();
    g.viewerW = g.viewerH = 0;
    g.viewerFailed = false;
}

void viewerClose(GalleryUI& g) {
    viewerRelease(g);
    g.viewerOpen    = false;
    g.viewerRequest = false;
    g.viewerIdx     = -1;
}

// Bring the viewer's full-resolution texture in line with viewerIdx. Only ever holds one.
void viewerSync(GalleryUI& g, const std::vector<GalleryImage>& images) {
    const bool valid = (g.viewerIdx >= 0 && g.viewerIdx < static_cast<int>(images.size()));
    const std::string want = valid ? images[static_cast<size_t>(g.viewerIdx)].path : std::string();
    if (want == g.viewerPath && (g.viewerTex || g.viewerFailed || want.empty())) return;

    viewerRelease(g);
    g.viewerPath = want;
    if (want.empty()) return;
    g.viewerTex = ::reveng::loadTexFromFile(want, &g.viewerW, &g.viewerH);
    g.viewerFailed = (g.viewerTex == 0);
}

// Remove one frame from the capture set: report it, erase it, and repair every index that pointed
// past it (selection + viewer). The thumbnail is cache-owned, so dropping the reference is all the
// texture bookkeeping required.
void cullImage(std::vector<GalleryImage>& images, int idx,
               GalleryActions& out, RevEngState& s, GalleryUI& g) {
    if (idx < 0 || idx >= static_cast<int>(images.size())) return;
    out.removed.push_back(images[static_cast<size_t>(idx)].path);
    images.erase(images.begin() + idx);

    if (s.selectedImage == idx)      s.selectedImage = -1;
    else if (s.selectedImage > idx)  --s.selectedImage;

    if (g.viewerIdx == idx) {
        if (images.empty()) viewerClose(g);
        else                g.viewerIdx = std::min(idx, static_cast<int>(images.size()) - 1);
    } else if (g.viewerIdx > idx) {
        --g.viewerIdx;
    }
}

void drawViewerModal(GalleryUI& g, std::vector<GalleryImage>& images,
                     GalleryActions& out, RevEngState& s) {
    if (g.viewerRequest) {
        ImGui::OpenPopup(kViewerPopup);
        g.viewerRequest = false;
        g.viewerOpen    = true;
    }
    if (!g.viewerOpen) return;

    if (images.empty()) { viewerClose(g); return; }
    if (g.viewerIdx < 0 || g.viewerIdx >= static_cast<int>(images.size()))
        g.viewerIdx = std::max(0, std::min(g.viewerIdx, static_cast<int>(images.size()) - 1));

    const ImVec2 wantSize(metrics::reThumbLg() * 4.0f, metrics::reThumbLg() * 3.0f);
    ImGui::SetNextWindowSize(wantSize, ImGuiCond_Appearing);

    bool stayOpen = true;
    if (!ImGui::BeginPopupModal(kViewerPopup, &stayOpen, ImGuiWindowFlags_NoSavedSettings)) {
        viewerClose(g);   // dismissed with Esc / click-away
        return;
    }
    if (!stayOpen) {
        ImGui::CloseCurrentPopup();
        ImGui::EndPopup();
        viewerClose(g);
        return;
    }

    viewerSync(g, images);
    GalleryImage& img   = images[static_cast<size_t>(g.viewerIdx)];
    const int     total = static_cast<int>(images.size());

    // ---- nav row -------------------------------------------------------------------------
    const bool canPrev = (g.viewerIdx > 0);
    const bool canNext = (g.viewerIdx + 1 < total);
    if (kit::SecondaryButton("< Prev", canPrev, "This is the first photo in the set.",
                             ImVec2(metrics::reBtnW(), metrics::reBtnH())))
        --g.viewerIdx;
    ImGui::SameLine(0.0f, metrics::reCtlGap());
    if (kit::SecondaryButton("Next >", canNext, "This is the last photo in the set.",
                             ImVec2(metrics::reBtnW(), metrics::reBtnH())))
        ++g.viewerIdx;
    ImGui::SameLine(0.0f, metrics::reSectionGap());
    kit::StatusChip(qualityVerdict(img), levelFor(img),
                    "Quality comes from the worker's add-images / coverage scoring, not the GUI.");

    if (ImGui::IsKeyPressed(ImGuiKey_LeftArrow)  && canPrev) --g.viewerIdx;
    if (ImGui::IsKeyPressed(ImGuiKey_RightArrow) && canNext) ++g.viewerIdx;

    kit::GapControl();

    // ---- hero ----------------------------------------------------------------------------
    // Reserve the metadata block + action row, give everything else to the image.
    const float reserved = ImGui::GetFrameHeightWithSpacing() * 5.0f
                         + metrics::reBtnH() + metrics::reSectionGap();
    ImVec2 box = ImGui::GetContentRegionAvail();
    box.y -= reserved;
    if (box.y < metrics::reThumbLg()) box.y = metrics::reThumbLg();

    char caption[320];
    std::snprintf(caption, sizeof caption, "%s  \xc2\xb7  #%d of %d",
                  baseName(img.path).c_str(), g.viewerIdx + 1, total);
    kit::HeroPreview("##re_gal_hero", g.viewerTex, g.viewerW, g.viewerH, box, caption,
                     g.viewerFailed ? "Could not decode this file" : "Loading full resolution\xe2\x80\xa6");

    kit::GapControl();

    // ---- metadata ---------------------------------------------------------------------------
    char val[192];
    if (img.srcW > 0 && img.srcH > 0) std::snprintf(val, sizeof val, "%d x %d px", img.srcW, img.srcH);
    else                              std::snprintf(val, sizeof val, "unknown");
    kit::KeyValueRow("Pixels", val);

    if (img.sharpness >= 0.0) std::snprintf(val, sizeof val, "%.1f", img.sharpness);
    else                      std::snprintf(val, sizeof val, "not scored");
    kit::KeyValueRow("Sharpness", val, img.sharpness >= 0.0 ? kit::Level::Neutral : kit::Level::Warn);

    kit::KeyValueRow("Quality", qualityVerdict(img), levelFor(img));
    kit::KeyValueRow("Path", img.path.c_str());

    if (img.synthetic || img.quality == "synthetic")
        kit::HonestyTag("approximate",
                        "Synthetic frame from a stub camera. Reference only - nothing measured "
                        "from this image is dimensionally meaningful.");

    kit::GapSection();

    // ---- actions ------------------------------------------------------------------------------
    const bool alreadySelected = (s.selectedImage == g.viewerIdx);
    if (kit::PrimaryButton(alreadySelected ? "Selected for Scale" : "Use for Scale",
                           !alreadySelected, "This photo is already the selected frame.",
                           ImVec2(metrics::reBtnW() * 1.6f, metrics::reBtnH())))
        s.selectedImage = g.viewerIdx;

    ImGui::SameLine(0.0f, metrics::reCtlGap());
    if (kit::SecondaryButton("Show in file manager", true, nullptr,
                             ImVec2(metrics::reBtnW() * 1.8f, metrics::reBtnH()))) {
        ui::revealInFileManager(img.path);
        out.revealed.push_back(img.path);
    }

    ImGui::SameLine(0.0f, metrics::reCtlGap());
    const int cullIdx = g.viewerIdx;
    if (kit::DangerButton("Remove from set")) {
        cullImage(images, cullIdx, out, s, gui());
        if (images.empty()) {
            ImGui::CloseCurrentPopup();
            ImGui::EndPopup();
            viewerClose(g);
            return;
        }
    }

    ImGui::EndPopup();
}

// ---- drag payload ----------------------------------------------------------------------------

void parseDropPayload(const ImGuiPayload* pl, std::vector<std::string>& outPaths) {
    if (!pl || !pl->Data || pl->DataSize <= 0) return;
    const char* data = static_cast<const char*>(pl->Data);
    const char* p    = data;
    while (p && *p) {
        const char* nl = std::strchr(p, '\n');
        const std::string line(p, nl ? nl : p + std::strlen(p));
        if (!line.empty()) outPaths.push_back(line);
        if (!nl) break;
        p = nl + 1;
    }
}

}  // namespace

// -----------------------------------------------------------------------------------------------
// Thumbnail loading
// -----------------------------------------------------------------------------------------------

void galleryLoadPendingThumbs(std::vector<GalleryImage>& images) {
    // One bounded edge for the whole sheet: big enough for the hover magnifier, small enough that
    // a 4000x3000 JPEG lands as a ~150 KB texture instead of ~48 MB.
    const int maxEdge = static_cast<int>(metrics::reThumbLg());

    // Keep the live set resident when it fits under the hard ceiling — otherwise the LRU would
    // evict tiles that are still on screen and we would re-decode them every frame.
    const int need = static_cast<int>(images.size()) + kMaxThumbDecodesPerFrame;
    if (need > ::reveng::thumbCacheLimit() && need <= kThumbCacheHardMax)
        ::reveng::setThumbCacheLimit(need);

    // Revalidate held ids only when the cache actually destroyed something since last frame. GL
    // recycles texture names, so a stale id can alias a DIFFERENT photo — in a contact sheet whose
    // job is telling frames apart, showing the wrong thumbnail is a correctness bug. The epoch
    // makes the check free in the overwhelmingly common no-eviction case.
    GalleryUI& g = gui();
    const unsigned long long epoch = ::reveng::thumbCacheEvictionEpoch();
    const bool revalidate = (epoch != g.thumbEpoch);
    g.thumbEpoch = epoch;

    int budget = kMaxThumbDecodesPerFrame;
    for (auto& img : images) {
        if (revalidate && img.texId &&
            !::reveng::thumbCacheStillMaps(img.path, maxEdge, img.texId))
            img.texId = 0;
        if (img.texId || img.path.empty() || img.texFailed) continue;

        const bool hit = ::reveng::thumbCacheHas(img.path, maxEdge);
        if (!hit && budget <= 0) continue;   // decode it on a later frame; the tile shows a slot

        const unsigned int tex = ::reveng::cachedThumbTex(img.path, maxEdge,
                                                          &img.thumbW, &img.thumbH,
                                                          &img.srcW, &img.srcH);
        if (!hit) --budget;
        if (!tex) { img.texFailed = true; continue; }
        img.texId = tex;
    }
}

void galleryFreeTextures(std::vector<GalleryImage>& images) {
    GalleryUI& g = gui();
    viewerClose(g);   // a session switch must not leave the viewer pointing into a cleared vector
    for (auto& img : images) {
        // Cache-owned ids: freeTexture() is a documented no-op for those, so this detaches the
        // reference without ever double-freeing a texture the cache still serves to someone else.
        ::reveng::freeTexture(img.texId);
        img.texId    = 0;
        img.thumbW   = 0;
        img.thumbH   = 0;
        img.texFailed = false;
    }
}

// -----------------------------------------------------------------------------------------------
// Main draw
// -----------------------------------------------------------------------------------------------

void drawImageGallery(AppState& st,
                      const std::string& session_id,
                      std::vector<GalleryImage>& images,
                      GalleryActions& out) {
    (void)st;   // the gallery issues no RPC; it reports intents through `out`

    out.dropped.clear();
    out.removed.clear();
    out.revealed.clear();

    GalleryUI&     g   = gui();
    RevEngState&   s   = S();
    const Palette& pal = ui::palette;

    const float tile = metrics::reThumbMd();
    const float gap  = metrics::reThumbGap();
    const Counts c   = tally(images);

    // Keep the shared selection index honest even if the set shrank behind our back.
    if (s.selectedImage >= static_cast<int>(images.size())) s.selectedImage = -1;

    int cullRequest = -1;   // index the user asked to remove this frame

    if (kit::CardBegin("re_gallery", "Capture set", nullptr, /*collapsible=*/true, &g.cardOpen)) {
        // ---- header verdict ------------------------------------------------------------------
        {
            const char* chip = "ready";
            kit::Level  lvl  = kit::Level::Ok;
            const char* why  = "Every frame is scored sharp.";
            if (c.total == 0) {
                chip = "empty"; lvl = kit::Level::Warn;
                why  = "No photos in this session yet.";
            } else if (c.synth > 0) {
                chip = "synthetic frames"; lvl = kit::Level::Error;
                why  = "Some frames came from a stub camera. They carry no real geometry - "
                       "cull them before reconstructing.";
            } else if (c.unreadable > 0) {
                chip = "unreadable files"; lvl = kit::Level::Error;
                why  = "Some files could not be decoded. Check the paths or re-add them.";
            } else if (c.blurred + c.dup > 0) {
                chip = "needs culling"; lvl = kit::Level::Warn;
                why  = "Blurred or duplicate frames cost reconstruction time and add no parallax.";
            } else if (c.unrated == c.total) {
                chip = "unrated"; lvl = kit::Level::Neutral;
                why  = "The worker has not scored these frames yet - add them to a session to "
                       "get sharpness and duplicate detection.";
            }
            kit::CardHeaderRight(ImGui::CalcTextSize(chip).x + metrics::reCardPadX() * 3.0f);
            kit::StatusChip(chip, lvl, why);
        }

        // ---- filter + summary ------------------------------------------------------------------
        kit::SegmentedControl("##re_gal_filter", kFilterLabels, FilterCount, &g.filter,
                              metrics::reBtnW() * 3.0f);
        ImGui::SameLine(0.0f, metrics::reCtlGap());
        ImGui::TextDisabled("%s", summaryText(c).c_str());
        ImGui::SameLine(0.0f, metrics::reLabelGap());
        kit::HelpMarker("Photogrammetry is only as good as the frames you keep. Filter to Blurred "
                        "or Duplicates and remove what you find - a smaller, sharper set "
                        "reconstructs faster AND more accurately than a big sloppy one.");

        if (session_id.empty() && !images.empty()) {
            kit::InlineStatus("No active session - these photos are not attached to a "
                              "reverse-engineering run. Pick or create a session in the rail.",
                              kit::Level::Warn);
        }

        kit::GapControl();

        // ---- filtered view ----------------------------------------------------------------------
        g.shown.clear();
        for (int i = 0; i < static_cast<int>(images.size()); ++i)
            if (passesFilter(images[static_cast<size_t>(i)], g.filter)) g.shown.push_back(i);
        const int nShown = static_cast<int>(g.shown.size());

        // ---- grid geometry ------------------------------------------------------------------------
        // Columns are computed from the width the tiles will ACTUALLY get: the card's available
        // width less the child's own padding, border and a permanently reserved scrollbar. Without
        // the reservation the last column pops in and out as the sheet crosses the overflow point.
        const float availW = ImGui::GetContentRegionAvail().x;
        float innerW = availW - metrics::reCardPadX() - metrics::stroke(2.0f) * 2.0f
                     - ImGui::GetStyle().ScrollbarSize;
        if (innerW < tile) innerW = tile;
        int cols = static_cast<int>((innerW + gap) / (tile + gap));
        if (cols < 1) cols = 1;

        const float rowH  = tile + ImGui::GetTextLineHeightWithSpacing() + gap;
        const int   rows  = (nShown + cols - 1) / cols;
        float gridH = (rows > 0 ? static_cast<float>(rows) : 1.0f) * rowH + metrics::reCardPadY();
        const float gridMaxH = metrics::reThumbLg() * 2.0f;
        if (gridH > gridMaxH) gridH = gridMaxH;
        // Empty / blocked states need room for an icon, a title and a wrapped paragraph.
        if (images.empty() || nShown == 0) gridH = metrics::reThumbLg() * 1.4f;

        ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding,
                            ImVec2(metrics::reCardPadX() * 0.5f, metrics::reCardPadY() * 0.5f));
        ImGui::BeginChild("##re_gal_grid", ImVec2(0.0f, gridH), ImGuiChildFlags_Border);
        ImGui::PopStyleVar();

        const ImVec2 gridMin = ImGui::GetWindowPos();
        const ImVec2 gridMax(gridMin.x + ImGui::GetWindowSize().x,
                             gridMin.y + ImGui::GetWindowSize().y);

        bool overActions = false;
        int  hoverIdx    = -1;
        ImVec2 hoverMin(0.0f, 0.0f), hoverMax(0.0f, 0.0f);

        if (images.empty()) {
            if (session_id.empty()) {
                kit::BlockedState(
                    "No session selected",
                    "Photos dropped here would have nothing to attach to.",
                    "Create or pick a session in the rail on the left, then add photos with the "
                    "webcam shutter, phone capture, the file browser, or a drag-drop.");
            } else if (kit::EmptyState(
                           "open", "No photos yet",
                           "Drag image files onto this panel, use the webcam shutter above, or "
                           "browse for them. Aim for 20-40 sharp photos orbiting the part with "
                           "about a third overlap between neighbours, and keep the scale mat in "
                           "frame - that overlap is what gives the reconstructor its parallax.",
                           "Browse for photos\xe2\x80\xa6")) {
                const std::string picked = ui::openFileDialog(
                    "Images (*.png *.jpg *.jpeg *.bmp)\0*.png;*.jpg;*.jpeg;*.bmp\0"
                    "All files (*.*)\0*.*\0");
                if (appendUnique(images, picked)) out.dropped.push_back(picked);
            }
        } else if (nShown == 0) {
            kit::EmptyState("ok_badge", "Nothing in this filter",
                            "No frame in this capture set carries that verdict. Switch back to "
                            "All to see the whole sheet.");
        } else {
            for (int k = 0; k < nShown; ++k) {
                const int i = g.shown[static_cast<size_t>(k)];
                GalleryImage& img = images[static_cast<size_t>(i)];
                ImGui::PushID(i);
                if (k % cols != 0) ImGui::SameLine(0.0f, gap);

                char label[32];
                std::snprintf(label, sizeof label, "#%d", i + 1);

                const bool selected = (s.selectedImage == i);
                const bool clicked  = kit::ThumbTile("##tile", img.texId, tile, selected,
                                                     badgeFor(img),
                                                     img.texFailed ? "unreadable" : label);
                const ImVec2 tmin = ImGui::GetItemRectMin();
                const ImVec2 tmax = ImGui::GetItemRectMax();
                const bool overTile = ImGui::IsWindowHovered() &&
                                      ImGui::IsMouseHoveringRect(tmin, tmax);

                // A synthetic or unreadable frame must be unmistakable at a glance, badge or not.
                ImDrawList* dl = ImGui::GetWindowDrawList();
                if (img.synthetic || img.quality == "synthetic") {
                    const ImVec4 scrim(pal.statusWarn[0], pal.statusWarn[1], pal.statusWarn[2], 0.22f);
                    dl->AddRectFilled(tmin, tmax, ImGui::ColorConvertFloat4ToU32(scrim),
                                      metrics::reCardRound());
                } else if (img.texFailed) {
                    const ImVec4 scrim(pal.statusErr[0], pal.statusErr[1], pal.statusErr[2], 0.22f);
                    dl->AddRectFilled(tmin, tmax, ImGui::ColorConvertFloat4ToU32(scrim),
                                      metrics::reCardRound());
                }

                if (clicked) s.selectedImage = i;

                if (overTile) {
                    hoverIdx = i;
                    hoverMin = tmin;
                    hoverMax = tmax;
                    if (ImGui::IsMouseDoubleClicked(ImGuiMouseButton_Left)) {
                        g.viewerIdx     = i;
                        g.viewerRequest = true;
                    }
                    // The magnifier is the whole point of a contact sheet; suppress it only while
                    // the pointer sits on the action strip (known one frame late — see GalleryUI).
                    if (!g.prevOverActions)
                        kit::ThumbHoverPreview(img.texId, img.thumbW, img.thumbH,
                                               metaLines(img, i, static_cast<int>(images.size())));
                }
                ImGui::PopID();
            }

            // ---- hover action strip ------------------------------------------------------------
            // Drawn after the grid so it cannot disturb SameLine anchoring, and so it wins the
            // hit-test against the tile underneath it.
            if (hoverIdx >= 0) {
                GalleryImage& img = images[static_cast<size_t>(hoverIdx)];
                const float bh    = metrics::reBtnH() * 0.72f;
                const float strip = hoverMax.x - hoverMin.x;
                const float bwL   = (strip - gap) * 0.42f;
                const float bwR   = (strip - gap) * 0.58f;

                const ImVec2 save = ImGui::GetCursorScreenPos();
                ImGui::PushID(hoverIdx);
                ImGui::SetCursorScreenPos(ImVec2(hoverMin.x, hoverMax.y - bh));
                if (kit::SecondaryButton("Show", true, nullptr, ImVec2(bwL, bh))) {
                    ui::revealInFileManager(img.path);
                    out.revealed.push_back(img.path);
                }
                overActions |= ImGui::IsItemHovered();
                ImGui::SameLine(0.0f, gap);
                if (kit::SecondaryButton("Remove", true, nullptr, ImVec2(bwR, bh)))
                    cullRequest = hoverIdx;
                overActions |= ImGui::IsItemHovered();
                ImGui::PopID();
                ImGui::SetCursorScreenPos(save);
            }
        }

        g.prevOverActions = overActions;

        // ---- drop target + highlight -----------------------------------------------------------
        if (ImGui::BeginDragDropTargetCustom(ImRect(gridMin, gridMax),
                                             ImGui::GetID("##re_gal_drop"))) {
            if (const ImGuiPayload* pl = ImGui::AcceptDragDropPayload(kFileDragPayload)) {
                std::vector<std::string> paths;
                parseDropPayload(pl, paths);
                for (const auto& p : paths)
                    if (appendUnique(images, p)) out.dropped.push_back(p);
            }
            ImGui::EndDragDropTarget();
        }

        {
            const ImGuiPayload* live = ImGui::GetDragDropPayload();
            if (live && live->IsDataType(kFileDragPayload)) {
                const bool over = ImGui::IsMouseHoveringRect(gridMin, gridMax, /*clip=*/false);
                ImDrawList* dl  = ImGui::GetWindowDrawList();
                // Inset by the stroke: a rect drawn exactly on the child bounds is half-clipped.
                const float ins = metrics::stroke(2.0f);
                const ImVec2 hi0(gridMin.x + ins, gridMin.y + ins);
                const ImVec2 hi1(gridMax.x - ins, gridMax.y - ins);
                const ImVec4 edge(pal.accent[0], pal.accent[1], pal.accent[2], over ? 0.95f : 0.45f);
                if (over) {
                    const ImVec4 fill(pal.accent[0], pal.accent[1], pal.accent[2], 0.12f);
                    dl->AddRectFilled(hi0, hi1, ImGui::ColorConvertFloat4ToU32(fill),
                                      metrics::reCardRound());
                }
                dl->AddRect(hi0, hi1, ImGui::ColorConvertFloat4ToU32(edge),
                            metrics::reCardRound(), 0, ins);
                if (over) {
                    const char* cap = "Drop to add these photos";
                    const ImVec2 tsz = ImGui::CalcTextSize(cap);
                    dl->AddText(ImVec2((gridMin.x + gridMax.x - tsz.x) * 0.5f,
                                       (gridMin.y + gridMax.y - tsz.y) * 0.5f),
                                Palette::toU32(pal.text), cap);
                }
            }
        }

        ImGui::EndChild();

        // ---- footer -----------------------------------------------------------------------------
        kit::GapControl();
        char cullLabel[64];
        std::snprintf(cullLabel, sizeof cullLabel, "Remove %d blurred", c.blurred);
        if (kit::SecondaryButton(cullLabel, c.blurred > 0,
                                 "No frame in this set is marked blurred.",
                                 ImVec2(metrics::reBtnW() * 1.5f, metrics::reBtnH()))) {
            for (int i = static_cast<int>(images.size()) - 1; i >= 0; --i)
                if (std::strcmp(badgeFor(images[static_cast<size_t>(i)]), "blurred") == 0)
                    cullImage(images, i, out, s, g);
        }
        ImGui::SameLine(0.0f, metrics::reCtlGap());
        std::snprintf(cullLabel, sizeof cullLabel, "Remove %d synthetic", c.synth);
        if (kit::SecondaryButton(cullLabel, c.synth > 0,
                                 "No synthetic frames in this set - every photo is real.",
                                 ImVec2(metrics::reBtnW() * 1.6f, metrics::reBtnH()))) {
            for (int i = static_cast<int>(images.size()) - 1; i >= 0; --i)
                if (std::strcmp(badgeFor(images[static_cast<size_t>(i)]), "synthetic") == 0)
                    cullImage(images, i, out, s, g);
        }
        ImGui::SameLine(0.0f, metrics::reSectionGap());
        ImGui::TextDisabled("Click a tile to select \xc2\xb7 double-click to open");
    }
    kit::CardEnd();

    // Deferred so the erase never happens mid-layout.
    if (cullRequest >= 0) cullImage(images, cullRequest, out, s, g);

    // The viewer lives outside the card: collapsing the card must not strand an open modal, and
    // OpenPopup/BeginPopupModal must share one ID-stack scope.
    drawViewerModal(g, images, out, s);
}

}  // namespace reveng
}  // namespace ui
