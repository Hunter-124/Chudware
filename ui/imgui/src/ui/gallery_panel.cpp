// T-U9 Sample Gallery panel — see gallery_panel.h.
//
// Architecture (pattern library §0/§1, mirrors parts_panel.cpp/T-U2):
//   - View-model: aicad::ui::GalleryModel (ui/abstract/gallery_view.h, no ImGui/GL includes)
//   - Scaffold: ui::drawPanelScaffold / ui::drawPanelEmptyState (T-U1 kit)
//   - Data source: samples/index.json (T-Q3), read once at first draw (small, local file —
//     no worker round-trip needed, unlike parts_panel's live part_search)
//   - Open: s.engine.open(path) (same call file_explorer.cpp's requestOpen ultimately makes),
//     queued + retried every frame until s.engineReady rather than fired once and dropped —
//     first-run's "open a sample" checkbox can fire before the worker has finished starting.
//   - Tour: spawns `runtimePython() <tour_script>` on a detached thread (same async pattern as
//     first_run.cpp's triggerRuntimeRepair), reports pass/fail via toast + panel status line.

#include "gallery_panel.h"
#include "panel_scaffold.h"
#include "gallery_view.h"   // ui/abstract/ (no ImGui includes there)

#include "app/app_state.h"
#include "bootstrap.h"
#include "toast.h"

#include "theme/theme.h"
#include "theme/texture_cache.h"
#include "theme/tokens.h"

#include "imgui.h"
#include <nlohmann/json.hpp>

#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <thread>

namespace ui {

namespace {

namespace fs = std::filesystem;

bool gOpen = false;
ui::PanelScaffoldState gScaffoldState;
aicad::ui::GalleryModel gModel;
bool gLoaded = false;   // samples/index.json parse attempted (once)

// Pending doc_open request (see requestOpenSample/tickGalleryOpenRequest).
std::string gPendingOpenPath;

// docs/contracts/fixtures/box.aicm-style candidate search — the samples/ dir may be reached
// from several relative depths depending on the process's CWD (dev run vs packaged AppImage).
bool findSamplesDir(fs::path& out) {
    const char* candidates[] = {"samples", "../samples", "../../samples", "../../../samples"};
    for (const char* c : candidates) {
        fs::path p(c);
        if (fs::is_directory(p) && fs::exists(p / "index.json")) { out = fs::absolute(p); return true; }
    }
    return false;
}

void loadSamples() {
    gLoaded = true;
    gModel.samples.clear();

    fs::path samplesDir;
    if (!findSamplesDir(samplesDir)) {
        gModel.state    = aicad::ui::GalleryState::Error;
        gModel.errorMsg = "samples/index.json not found";
        return;
    }

    std::ifstream f(samplesDir / "index.json");
    if (!f) {
        gModel.state    = aicad::ui::GalleryState::Error;
        gModel.errorMsg = "could not open samples/index.json";
        return;
    }

    nlohmann::json j;
    try {
        f >> j;
    } catch (const std::exception& e) {
        gModel.state    = aicad::ui::GalleryState::Error;
        gModel.errorMsg = std::string("malformed samples/index.json: ") + e.what();
        return;
    }

    if (!j.contains("samples") || !j["samples"].is_array()) {
        gModel.state    = aicad::ui::GalleryState::Error;
        gModel.errorMsg = "samples/index.json has no 'samples' array";
        return;
    }

    for (const auto& rec : j["samples"]) {
        aicad::ui::GallerySample sample;
        sample.id          = rec.value("id", std::string());
        sample.title       = rec.value("title", std::string());
        sample.description = rec.value("description", std::string());
        sample.discipline  = rec.value("discipline", std::string());

        const std::string docFile   = rec.value("doc_file", std::string());
        const std::string thumbFile = rec.value("thumbnail", std::string());
        if (!docFile.empty())   sample.docFile      = (samplesDir / docFile).string();
        if (!thumbFile.empty()) sample.thumbnailPath = (samplesDir / thumbFile).string();

        if (rec.contains("what_it_teaches") && rec["what_it_teaches"].is_array())
            for (const auto& t : rec["what_it_teaches"])
                if (t.is_string()) sample.whatItTeaches.push_back(t.get<std::string>());
        if (rec.contains("tags") && rec["tags"].is_array())
            for (const auto& t : rec["tags"])
                if (t.is_string()) sample.tags.push_back(t.get<std::string>());

        const fs::path tourPath = samplesDir / "tours" / (sample.id + "_tour.py");
        if (fs::exists(tourPath)) {
            sample.hasTour    = true;
            sample.tourScript = tourPath.string();
        }

        gModel.samples.push_back(std::move(sample));
    }

    gModel.state = aicad::ui::GalleryState::Ready;
}

// T-V3-style note to self: do NOT write to gModel (read every frame by the render thread)
// from the detached thread below — only postToast(), which is its own mutex-guarded queue
// (toast.h), is safe to call cross-thread. gModel.tourStatusMsg is set synchronously here
// (main thread) and again only by the main thread reacting to a toast, never by the thread.
void launchTour(const aicad::ui::GallerySample& sample) {
    gModel.tourStatusMsg = "Running tour \"" + sample.title + "\"...";
    postToast(ToastLevel::Info, "Running the " + sample.title + " tour (headless)...");

    std::string script = sample.tourScript;
    std::string title  = sample.title;
    std::thread([script, title]() {
        const std::string py = runtimePython();
        const std::string cmd = "\"" + py + "\" \"" + script + "\" >/dev/null 2>&1";
        const int rc = std::system(cmd.c_str());
        const bool ok = (rc == 0);
        const std::string msg = ok ? ("Tour \"" + title + "\" completed")
                                    : ("Tour \"" + title + "\" failed (exit " + std::to_string(rc) + ")");
        postToast(ok ? ToastLevel::Info : ToastLevel::Error, msg);
    }).detach();
}

// ---- content -----------------------------------------------------------------------------

void drawThumbGrid(app::AppState& s) {
    const float S = theme::S();
    const float cellW = 148.f * S;
    const float cellH = 168.f * S;
    const float avail = ImGui::GetContentRegionAvail().x;
    const int cols = avail > cellW ? (int)(avail / (cellW + 8.f * S)) : 1;

    for (int i = 0; i < (int)gModel.samples.size(); ++i) {
        const auto& sample = gModel.samples[i];
        const bool selected = (gModel.selectedIdx == i);

        ImGui::PushID(i);
        ImGui::BeginGroup();

        const ImVec2 cellMn = ImGui::GetCursorScreenPos();
        theme::glassRowBackground(ImGui::GetWindowDrawList(), cellMn,
                                   ImVec2(cellMn.x + cellW, cellMn.y + cellH),
                                   6.f * S, false, selected);

        ImGui::Dummy(ImVec2(cellW, 4.f * S));
        ImGui::Indent(6.f * S);

        int tw = 0, th = 0;
        ImTextureID tex = sample.thumbnailPath.empty()
                              ? (ImTextureID)0
                              : loadPreviewTexture(sample.thumbnailPath, &tw, &th);
        const float imgW = cellW - 12.f * S;
        if (tex && tw > 0 && th > 0) {
            const float imgH = imgW * (float)th / (float)tw;
            ImGui::Image(tex, ImVec2(imgW, imgH));
        } else {
            ImGui::Dummy(ImVec2(imgW, imgW * 0.75f));
            ImGui::SetCursorScreenPos(ImVec2(ImGui::GetCursorScreenPos().x, ImGui::GetCursorScreenPos().y - imgW * 0.75f));
            ImGui::TextColored(TVEC(txt2), "  (no thumbnail)");
            ImGui::Dummy(ImVec2(imgW, imgW * 0.75f));
        }

        ImGui::TextColored(TVEC(accent), "[%s]", aicad::ui::galleryDisciplineLabel(sample.discipline));
        ImGui::PushTextWrapPos(ImGui::GetCursorPosX() + imgW);
        ImGui::TextUnformatted(sample.title.c_str());
        ImGui::PopTextWrapPos();

        ImGui::Unindent(6.f * S);
        ImGui::EndGroup();

        if (ImGui::IsItemClicked()) gModel.selectedIdx = i;

        if ((i + 1) % (cols > 0 ? cols : 1) != 0 && i + 1 < (int)gModel.samples.size())
            ImGui::SameLine(0, 8.f * S);
        else
            ImGui::Dummy(ImVec2(0, 6.f * S));

        ImGui::PopID();
    }
}

void drawDetail(app::AppState& /*s*/) {
    const float S = theme::S();
    if (gModel.selectedIdx < 0 || gModel.selectedIdx >= (int)gModel.samples.size()) {
        drawPanelEmptyState("Select a sample", "to see what it teaches");
        return;
    }
    const auto& sample = gModel.samples[gModel.selectedIdx];

    ImGui::Dummy(ImVec2(0, 4.f * S));
    ImGui::Separator();
    ImGui::Dummy(ImVec2(0, 4.f * S));

    ImGui::PushFont(theme::font(theme::Font::H2));
    ImGui::TextUnformatted(sample.title.c_str());
    ImGui::PopFont();
    ImGui::SameLine();
    ImGui::TextColored(TVEC(accent), " [%s]", aicad::ui::galleryDisciplineLabel(sample.discipline));

    ImGui::PushTextWrapPos(ImGui::GetCursorPosX() + ImGui::GetContentRegionAvail().x);
    ImGui::TextColored(TVEC(txt2), "%s", sample.description.c_str());
    ImGui::PopTextWrapPos();
    ImGui::Dummy(ImVec2(0, 6.f * S));

    if (!sample.whatItTeaches.empty()) {
        ImGui::TextColored(TVEC(txt1), "What it teaches:");
        for (const auto& line : sample.whatItTeaches) {
            ImGui::PushTextWrapPos(ImGui::GetCursorPosX() + ImGui::GetContentRegionAvail().x);
            ImGui::TextColored(TVEC(txt2), "  - %s", line.c_str());  // ASCII — the font atlas
                                                                      // doesn't cover U+2022
            ImGui::PopTextWrapPos();
        }
        ImGui::Dummy(ImVec2(0, 8.f * S));
    }

    if (ImGui::Button("Open", ImVec2(120.f * S, 0.f))) requestOpenSample(sample.docFile);
    ImGui::SameLine();
    if (!sample.hasTour) ImGui::BeginDisabled();
    if (ImGui::Button("Run guided tour", ImVec2(160.f * S, 0.f))) launchTour(sample);
    if (!sample.hasTour) ImGui::EndDisabled();

    if (!gModel.tourStatusMsg.empty())
        ImGui::TextColored(TVEC(txt2), "%s", gModel.tourStatusMsg.c_str());

    switch (gModel.openState) {
    case aicad::ui::GalleryOpenState::WaitingForEngine:
        ImGui::TextColored(TVEC(txt2), "Waiting for the engine to start...");
        break;
    case aicad::ui::GalleryOpenState::Opened:
        ImGui::TextColored(TVEC(good), "Opened.");
        break;
    case aicad::ui::GalleryOpenState::Error:
        ImGui::TextColored(TVEC(bad), "Open failed: %s", gModel.openErrorMsg.c_str());
        break;
    default:
        break;
    }
}

}  // namespace

void drawGalleryPanel(app::AppState& s) {
    if (!gLoaded) loadSamples();

    ui::PanelScaffoldSpec spec;
    spec.id    = "gallery";
    spec.title = "Sample Gallery";

    ui::drawPanelScaffold(spec, gScaffoldState, [&]() {
        if (gModel.state == aicad::ui::GalleryState::Error) {
            drawPanelEmptyState("Gallery unavailable", gModel.errorMsg.c_str());
            return;
        }
        if (gModel.samples.empty()) {
            drawPanelEmptyState("No samples found");
            return;
        }
        const float avail = ImGui::GetContentRegionAvail().y;
        ImGui::BeginChild("##galleryGrid", ImVec2(0, avail * 0.4f), ImGuiChildFlags_None);
        drawThumbGrid(s);
        ImGui::EndChild();
        drawDetail(s);
    });
}

void openGalleryPanel() { gOpen = true; }

bool* galleryPanelOpen() { return &gOpen; }

void requestOpenSample(const std::string& docAbsPath) {
    if (docAbsPath.empty()) return;
    gPendingOpenPath      = docAbsPath;
    gModel.openState      = aicad::ui::GalleryOpenState::WaitingForEngine;
    gModel.openErrorMsg.clear();
}

void selectAndOpenFirstSampleForShot() {
    if (!gLoaded) loadSamples();   // scenario seam — fires before the panel's own first draw
    if (gModel.samples.empty()) return;
    gModel.selectedIdx = 0;
    requestOpenSample(gModel.samples[0].docFile);
}

void tickGalleryOpenRequest(app::AppState& s) {
    if (gPendingOpenPath.empty()) return;
    if (!s.engineReady) return;   // keep waiting — polled every frame, never silently dropped

    gModel.openState = aicad::ui::GalleryOpenState::Opening;
    const std::string path = gPendingOpenPath;
    gPendingOpenPath.clear();

    if (s.engine.open(path)) {
        s.onDocMutation();
        s.status = "Opened " + path;
        gModel.openState = aicad::ui::GalleryOpenState::Opened;
    } else {
        gModel.openState   = aicad::ui::GalleryOpenState::Error;
        gModel.openErrorMsg = "engine.open() failed for " + path;
    }
}

}  // namespace ui
