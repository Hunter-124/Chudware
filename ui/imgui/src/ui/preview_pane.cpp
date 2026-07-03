// Universal preview pane (T-F16). See preview_pane.h.
#include "preview_pane.h"

#include "artifact_cards.h"
#include "platform/open_external.h"
#include "platform/path_predicates.h"
#include "theme/texture_cache.h"
#include "theme/theme.h"

#include "app/app_state.h"

#include "imgui.h"

#include <nlohmann/json.hpp>

#include <filesystem>

namespace ui {

namespace {

std::vector<aicad::ui::ArtifactCard> gCards;
aicad::ui::PreviewModel gPreview;
std::string gProjectDir;
bool gFocusReq = false;
bool gPaneOpen = false;   // pane is closable; opened on demand (explorer/scenario/agent)
bool gListPending = false;

std::string stemOf(const std::string& path) {
    if (path.empty()) return path;
    std::filesystem::path p(path);
    std::string stem = p.stem().string();
    return stem.empty() ? path : stem;
}

// isAbsolutePath lives in platform/paths.h (OS branching belongs in the platform layer).
using platform::isAbsolutePath;

// pages[].image.path / artifacts[].path are project-relative (contract deliverables/1 §5.4/§5.5)
// — join with the absolute project_dir the worker returned alongside them.
std::string joinAbs(const std::string& projectDir, const std::string& relOrAbs) {
    if (relOrAbs.empty() || isAbsolutePath(relOrAbs) || projectDir.empty()) return relOrAbs;
    return (std::filesystem::path(projectDir) / relOrAbs).string();
}

// Centers a (possibly wrapped) line of text within the current content region.
void centeredText(const std::string& text) {
    if (text.empty()) return;
    const float avail = ImGui::GetContentRegionAvail().x;
    const ImVec2 ts = ImGui::CalcTextSize(text.c_str(), nullptr, false, avail);
    const float x = ImGui::GetCursorPosX() + (avail - ts.x) * 0.5f;
    if (x > ImGui::GetCursorPosX()) ImGui::SetCursorPosX(x);
    ImGui::PushTextWrapPos(ImGui::GetCursorPosX() + avail);
    ImGui::TextUnformatted(text.c_str());
    ImGui::PopTextWrapPos();
}

void drawPreviewBody(app::AppState& /*s*/) {
    const float S = theme::S();

    if (gPreview.state == aicad::ui::PreviewState::Ready && !gPreview.pages.empty()) {
        int idx = gPreview.activePage;
        if (idx < 0) idx = 0;
        if (idx >= (int)gPreview.pages.size()) idx = (int)gPreview.pages.size() - 1;
        gPreview.activePage = idx;
        const aicad::ui::PreviewPage& pg = gPreview.pages[idx];

        int tw = 0, th = 0;
        ImTextureID tex = loadPreviewTexture(pg.imagePath, &tw, &th);
        if (tex && tw > 0 && th > 0) {
            const float availW = ImGui::GetContentRegionAvail().x;
            const float w = availW;
            const float h = w * (float)th / (float)tw;
            ImGui::Image(tex, ImVec2(w, h));
        } else {
            centeredText("Preview image missing");
        }

        if (gPreview.pages.size() > 1) {
            if (ImGui::SmallButton("Prev") && idx > 0) gPreview.activePage = idx - 1;
            ImGui::SameLine();
            ImGui::Text("Page %d/%d", idx + 1, (int)gPreview.pages.size());
            ImGui::SameLine();
            if (ImGui::SmallButton("Next") && idx < (int)gPreview.pages.size() - 1)
                gPreview.activePage = idx + 1;
        }
        ImGui::Dummy(ImVec2(0, 4.f * S));
        if (!gPreview.resourceLink.empty() && ImGui::SmallButton("Open externally"))
            openPathExternally(gPreview.resourceLink);
        return;
    }

    // Typed placeholder (Empty/Loading/Unsupported/ConverterMissing/NotFound/Error) — never blank.
    ImGui::Dummy(ImVec2(0, ImGui::GetContentRegionAvail().y * 0.4f));
    centeredText(aicad::ui::previewStateLabel(gPreview.state));
    if (!gPreview.message.empty()) {
        ImGui::Dummy(ImVec2(0, 4.f * S));
        centeredText(gPreview.message);
    }
}

}  // namespace

void refreshArtifactList(app::AppState& s) {
    if (gListPending) return;
    gListPending = true;
    s.rpc.call("list_artifacts", nlohmann::json::object(), [](const core::RpcResult& r) {
        gListPending = false;
        if (!r.ok || !r.result.value("ok", false)) return;

        const std::string pd = r.result.value("project_dir", std::string());
        if (!pd.empty()) gProjectDir = pd;

        gCards.clear();
        if (r.result.contains("artifacts") && r.result["artifacts"].is_array()) {
            for (const auto& rec : r.result["artifacts"]) {
                aicad::ui::ArtifactCard c;
                c.artifactId = rec.value("artifact_id", std::string());
                c.kind       = rec.value("kind", std::string());
                c.path       = rec.value("path", std::string());
                c.title      = rec.value("title", std::string());
                if (c.title.empty()) c.title = stemOf(c.path);
                c.createdTs  = rec.value("created_ts", std::string());
                c.runId      = rec.value("run_id", std::string());
                gCards.push_back(std::move(c));
            }
        }
    });
}

void requestArtifactPreview(app::AppState& s, const std::string& path) {
    gPreview = aicad::ui::PreviewModel{};
    gPreview.path = path;
    gPreview.state = aicad::ui::PreviewState::Loading;

    s.rpc.call("render_document", {{"path", path}}, [](const core::RpcResult& r) {
        if (!r.ok) {
            gPreview.state = aicad::ui::PreviewState::Error;
            gPreview.message = r.err;
            return;
        }
        const nlohmann::json& res = r.result;
        const std::string pd = res.value("project_dir", std::string());
        if (!pd.empty()) gProjectDir = pd;

        if (res.value("ok", false)) {
            gPreview.state = aicad::ui::PreviewState::Ready;
            gPreview.kind = res.value("kind", std::string());
            gPreview.resourceLink = res.value("resource_link", std::string());
            gPreview.activePage = 0;
            gPreview.pages.clear();
            if (res.contains("pages") && res["pages"].is_array()) {
                for (const auto& p : res["pages"]) {
                    aicad::ui::PreviewPage pg;
                    pg.index = p.value("index", 0);
                    if (p.contains("image") && p["image"].is_object()) {
                        pg.imagePath = joinAbs(gProjectDir, p["image"].value("path", std::string()));
                        pg.w = p["image"].value("w", 0);
                        pg.h = p["image"].value("h", 0);
                        // Fresh render → drop any stale/negative cache entry for this page PNG.
                        evictPreviewTexture(pg.imagePath);
                    }
                    gPreview.pages.push_back(std::move(pg));
                }
            }
        } else {
            gPreview.state = aicad::ui::previewStateFromReason(res.value("reason", std::string()));
            gPreview.message = res.value("hint", res.value("reason", std::string()));
        }
    });
}

void drawPreviewPane(app::AppState& s) {
    static bool inited = false;
    if (!inited) {
        inited = true;
        refreshArtifactList(s);
    }

    const float S = theme::S();
    const float leftW = 240.f * S;

    ImGui::BeginChild("##previewCards", ImVec2(leftW, 0), ImGuiChildFlags_Borders);
    if (ImGui::SmallButton("Refresh")) refreshArtifactList(s);
    ImGui::Separator();
    for (auto& card : gCards) {
        std::string thumb;
        if (gPreview.path == card.path && gPreview.state == aicad::ui::PreviewState::Ready &&
            !gPreview.pages.empty()) {
            const int idx = gPreview.activePage >= 0 && gPreview.activePage < (int)gPreview.pages.size()
                                ? gPreview.activePage
                                : 0;
            thumb = gPreview.pages[idx].imagePath;
        }

        ArtifactCardActions acts;
        acts.onPreview = [&s, path = card.path]() { requestArtifactPreview(s, path); };
        acts.onOpen = [&card]() {
            // card.path is project-relative (§3 record) — resolve against project_dir before
            // handing it to the OS opener; the resource_link (file:// URI) is already absolute.
            const std::string target =
                (!gPreview.resourceLink.empty() && gPreview.path == card.path)
                    ? gPreview.resourceLink
                    : joinAbs(gProjectDir, card.path);
            openPathExternally(target);
        };
        acts.onRegenerate = [&s, path = card.path]() {
            for (const auto& pg : gPreview.pages) evictPreviewTexture(pg.imagePath);
            requestArtifactPreview(s, path);
        };

        drawArtifactCard(card, thumb, acts);
        ImGui::Dummy(ImVec2(0, 6.f * S));
    }
    ImGui::EndChild();

    ImGui::SameLine();

    ImGui::BeginChild("##previewBody", ImVec2(0, 0), ImGuiChildFlags_Borders);
    drawPreviewBody(s);
    ImGui::EndChild();
}

void previewPaneSeedForShot(const std::vector<aicad::ui::ArtifactCard>& cards,
                             const aicad::ui::PreviewModel& model) {
    gCards = cards;
    gPreview = model;
    gPaneOpen = true;
    gFocusReq = true;
}

void requestPreviewFocus() { gFocusReq = true; }

void openPreviewPane() {
    gPaneOpen = true;
    gFocusReq = true;
}

bool* previewPaneOpen() { return &gPaneOpen; }

bool previewTakeFocusRequest() {
    const bool v = gFocusReq;
    gFocusReq = false;
    return v;
}

}  // namespace ui
