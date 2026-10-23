// PLM Project Hub — page bodies.
//
// Overview (dashboard + quick checkpoint), Files (items grid + track/link) and
// Team & Sync (remotes, one-button sync, advisory locks — T-R7) are implemented
// here. Every page reads only the cached rows on g_plmHub (filled by
// plm_state.cpp) and mutates via async st.rpc; on a successful mutation it bumps
// the generation so caches refetch.
#include "plm_hub.h"

#include <algorithm>
#include <cstdarg>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <ctime>
#include <functional>
#include <map>
#include <set>
#include <string>
#include <vector>

#include "imgui.h"

#include "core/app_state.h"
#include "core/async_rpc.h"
#include "ui/file_dialog.h"
#include "ui/icons.h"
#include "ui/metrics.h"
#include "ui/widgets/kit.h"
#include "ui_theme.h"
#include "util/time_local.h"

namespace ui {
namespace {

nlohmann::json pathParam(AppState& st) {
    nlohmann::json p;
    p["path"] = st.plmProjectPath();
    return p;
}

void accentButtonColors() {
    ImGui::PushStyleColor(ImGuiCol_Button,
        ImVec4(palette.accent[0], palette.accent[1], palette.accent[2], 0.85f));
    ImGui::PushStyleColor(ImGuiCol_ButtonHovered,
        ImVec4(palette.accent[0], palette.accent[1], palette.accent[2], 1.0f));
}

// ---- card tint by category (priority / severity / status) ---------------------
// Each list card gets a translucent liquid-glass background tinted by the item's
// standing. Only tasks/open-tasks have a real "priority"; the other lists map to
// their natural status colour (severity, lifecycle, scope), which reads the same.
// blue / yellow / red — low / med / high, per the agreed mapping (not the
// generic accent/dim/warn scheme used elsewhere in the hub).
const float kPriorityLow[4] = {0.420f, 0.620f, 0.960f, 1.0f};
const float* plmTintPriority(const std::string& pr) {
    if (pr == "p0" || pr == "p1") return palette.statusErr;  // critical / high -> red
    if (pr == "p3") return kPriorityLow;                     // low -> blue
    return palette.statusWarn;                                // p2 / med / default -> yellow
}
const float* plmTintSeverity(const std::string& sev) {
    if (sev == "critical" || sev == "high") return palette.statusErr;
    if (sev == "med") return palette.statusWarn;
    if (sev == "low") return palette.accentDim;
    return palette.textDim;
}
// p0 highest → smallest rank (sorts first). Unknown → med.
int plmPriorityRank(const std::string& pr) {
    if (pr == "p0") return 0; if (pr == "p1") return 1;
    if (pr == "p3") return 3; return 2;
}
int plmSeverityRank(const std::string& sev) {
    if (sev == "critical") return 0; if (sev == "high") return 1;
    if (sev == "med") return 2; return 3;
}

// A self-bounding "card": ui::kit::cardBegin IS this idiom — the wave-0 kit absorbed the
// AutoResizeY child, the deferred background draw-channel and the glassCardWash + tint gradient
// from right here. What stays local is what the kit has no parameter for: the SELECTED ring
// (ui::glassSelectionGlow, the app's one selected look) and the click-anywhere-in-the-card hit
// test plmCardEnd reports back for row selection.
static std::vector<bool> s_cardSel;

void plmCardBegin(const char* id, bool sel, float padX = 8.0f, float padY = 6.0f,
                  const float* tint = nullptr, float tintA = 0.20f) {
    // The pads the call sites pass are a density HINT, not free geometry: 3-5dp is the dense
    // activity/commit rhythm (Compact), 6dp the task/issue/release card rhythm (Cozy). From there
    // the padding, corner and wash are the kit's. tintA has no kit equivalent — the shared card
    // wash is one strength app-wide, and the tint HUE is what carries the priority/severity.
    ui::kit::pushDensity(padY <= 5.0f ? ui::kit::Density::Compact : ui::kit::Density::Cozy);
    (void)padX;
    (void)tintA;
    ui::kit::cardBegin(id, /*title=*/nullptr, /*icon=*/nullptr, /*open=*/nullptr,
                       sel ? palette.accent : tint);
    s_cardSel.push_back(sel);
}

// Close a card opened with plmCardBegin. Returns true if the card body was clicked outside any
// inner widget (for row/card selection).
bool plmCardEnd(float gap = 3.0f) {
    const bool sel = !s_cardSel.empty() && s_cardSel.back();
    if (!s_cardSel.empty()) s_cardSel.pop_back();

    // Hit test and rect must both be read while the card's child window is still current.
    const bool clicked = ImGui::IsWindowHovered() &&
                         ImGui::IsMouseClicked(ImGuiMouseButton_Left) &&
                         !ImGui::IsAnyItemHovered();
    if (sel) {
        // Drawn on the CONTENT channel so it lands above the wash cardEnd paints behind it; the
        // rings step inward from the edge, well outside the card's content padding.
        const ImVec2 mn = ImGui::GetWindowPos();
        const ImVec2 sz = ImGui::GetWindowSize();
        ui::glassSelectionGlow(ImGui::GetWindowDrawList(), mn,
                               ImVec2(mn.x + sz.x, mn.y + sz.y),
                               ui::kit::rowRound() / ui::uiScale, palette.accent);
    }
    ui::kit::cardEnd();
    ui::kit::popDensity();
    ImGui::Dummy(ImVec2(0, gap * ui::uiScale));
    return clicked;
}

// Dim helper text. Wraps to the content-region width so long paragraphs never
// run off the right edge (short inline uses after SameLine wrap harmlessly).
void dimText(const char* fmt, ...) {
    va_list ap; va_start(ap, fmt);
    ImGui::PushStyleColor(ImGuiCol_Text,
        ImVec4(palette.textDim[0], palette.textDim[1], palette.textDim[2], 1.0f));
    ImGui::TextWrappedV(fmt, ap);
    ImGui::PopStyleColor();
    va_end(ap);
}

// a small labelled count card
// A bordered, padded stat chip: the count centered up top, the label centered
// below. Fixed size so a row of them reads as a tidy dashboard band instead of
// numbers crammed against the left edge.
// `w` = chip width (0 = default). No trailing SameLine — the caller lays chips out
// (and wraps) so a band of them fits whatever column width it lives in.
void statCard(const char* label, int n, const float* col, float w = 0.0f) {
    const float S = ui::uiScale;
    const ImVec2 sz(w > 0 ? w : 100 * S, 60 * S);
    ImVec2 p0 = ImGui::GetCursorScreenPos();
    ImVec2 p1(p0.x + sz.x, p0.y + sz.y);
    ImDrawList* dl = ImGui::GetWindowDrawList();
    ui::glassBackground(dl, p0, p1, 10.0f);   // frosted translucent chip w/ shine
    char buf[24]; std::snprintf(buf, sizeof(buf), "%d", n);
    ImVec2 ns = ImGui::CalcTextSize(buf);
    dl->AddText(ImVec2(p0.x + (sz.x - ns.x) * 0.5f, p0.y + 11 * S),
        ImGui::GetColorU32(ImVec4(col[0], col[1], col[2], 1.0f)), buf);
    ImVec2 ls = ImGui::CalcTextSize(label);
    dl->AddText(ImVec2(p0.x + (sz.x - ls.x) * 0.5f, p1.y - ls.y - 9 * S),
        ImGui::GetColorU32(ImVec4(
            palette.textDim[0], palette.textDim[1], palette.textDim[2], 1.0f)), label);
    ImGui::Dummy(sz);
}

// (ISO timestamp shortening lives in plm_state.cpp as ui::plmNiceTime — the
// quickbar's presence tooltip spells lock expiry the same way this TU does.)

// Read-only text the user can select and copy (Ctrl-C / right-click). Styled to
// look like a plain label — transparent frame, no padding, full-width — but it's
// a real text field so highlighting and copy/paste work throughout the hub.
void plmCopyText(const char* id, const std::string& text, const float* col = nullptr) {
    const float* c = col ? col : palette.text;
    const float S = ui::uiScale;
    char buf[1024]; std::snprintf(buf, sizeof(buf), "%s", text.c_str());
    // A read-only field that reads like a plain label: a barely-there frame (so the
    // eye sees text, not a box) but real horizontal padding so the glyphs never jam
    // against the left edge, and vertical padding so the row has proper height. The
    // faint hover/active wash is the only affordance that it's selectable.
    ImGui::PushStyleColor(ImGuiCol_FrameBg, ImVec4(0, 0, 0, 0));
    ImGui::PushStyleColor(ImGuiCol_FrameBgHovered,
        ImVec4(palette.text[0], palette.text[1], palette.text[2], 0.05f));
    ImGui::PushStyleColor(ImGuiCol_FrameBgActive,
        ImVec4(palette.text[0], palette.text[1], palette.text[2], 0.07f));
    ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(c[0], c[1], c[2], 1.0f));
    ImGui::PushStyleVar(ImGuiStyleVar_FrameBorderSize, 0.0f);
    ImGui::PushStyleVar(ImGuiStyleVar_FramePadding, ImVec2(6 * S, 2 * S));
    ImGui::PushStyleVar(ImGuiStyleVar_FrameRounding, 5 * S);
    ImGui::SetNextItemWidth(-1);
    ImGui::InputText(id, buf, sizeof(buf), ImGuiInputTextFlags_ReadOnly);
    ImGui::PopStyleVar(3);
    ImGui::PopStyleColor(4);
}

// A wrapping item title/message. Every list card uses this so long text wraps to
// the card width (and grows the AutoResizeY card) instead of clipping — unlike
// plmCopyText, which is a single-line field. Plain text, so a click on it counts
// as a card-body click (used to select the row and reveal its admin controls).
// `col` = text colour (nullptr = default). Pass alignFrame when it shares a line
// with a framed widget (e.g. a severity badge) so baselines line up.
void plmItemTitle(const std::string& text, const float* col = nullptr,
                  bool alignFrame = false) {
    const float* c = col ? col : palette.text;
    if (alignFrame) ImGui::AlignTextToFramePadding();
    ImGui::PushTextWrapPos(0.0f);
    ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(c[0], c[1], c[2], 1.0f));
    ImGui::TextUnformatted(text.c_str());
    ImGui::PopStyleColor();
    ImGui::PopTextWrapPos();
}

// Tight, uniform metrics for a card's admin action row — shown only when the card
// is selected. Every card's controls use these so spacing/padding read identically.
void plmActionRowBegin() {
    const float S = ui::uiScale;
    ImGui::PushStyleVar(ImGuiStyleVar_FramePadding, ImVec2(4 * S, 1 * S));
    ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing, ImVec2(3 * S, 1 * S));
}
void plmActionRowEnd() { ImGui::PopStyleVar(2); }

// A discrete frosted-glass section box. Content drawn between Begin/End sits inside
// a rounded translucent panel with even internal padding and an optional icon+title
// header — so each settings/planning group reads as its own card instead of a wall
// of left-hugging text. Auto-height; pass width for side-by-side panels (0 = fill).
// Callers add vertical spacing between stacked panels themselves.
void plmPanelBegin(const char* id, const char* title, const char* icon = nullptr,
                   float width = 0.0f) {
    const float S = ui::uiScale;
    ui::pushGlassChildStyle(11.0f);
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(16 * S, 11 * S));
    ImGui::BeginChild(id, ImVec2(width, 0),
                      ImGuiChildFlags_AutoResizeY | ImGuiChildFlags_Borders);
    if (title && title[0]) {
        // Icon centred on the SAME frame height the title is aligned to, so the
        // glyph and text share a baseline instead of the icon floating high.
        const float h = ImGui::GetFrameHeight();
        if (icon && icon[0]) {
            ImVec2 c = ImGui::GetCursorScreenPos();
            const float isz = 17 * S;
            ui::drawIconGlass(ImGui::GetWindowDrawList(), icon,
                              ImVec2(c.x + isz * 0.5f, c.y + h * 0.5f), isz,
                              Palette::toU32(palette.accent), 0);
            ImGui::Dummy(ImVec2(isz + 9 * S, h));
            ImGui::SameLine();
        }
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(palette.accent[0], palette.accent[1], palette.accent[2], 1.0f));
        ImGui::AlignTextToFramePadding();
        ImGui::TextUnformatted(title);
        ImGui::PopStyleColor();
        ImGui::Dummy(ImVec2(0, 1 * S));  // sit the title close to its content
    }
}

void plmPanelEnd() {
    ImGui::EndChild();
    ImGui::PopStyleVar();      // WindowPadding
    ui::popGlassChildStyle();
}

void priorityDot(const std::string& pr);  // defined below; used by Overview too

void severityBadge(const std::string& sev) {
    ui::kit::Tone tone = ui::kit::Tone::Neutral;
    if (sev == "critical" || sev == "high") tone = ui::kit::Tone::Err;
    else if (sev == "med") tone = ui::kit::Tone::Warn;
    else if (sev == "low") tone = ui::kit::Tone::Info;
    ui::kit::chip(sev.empty() ? "med" : sev.c_str(), tone);
}

// Recent commits are rendered identically wherever the hub previews repository
// history. Callers own the surrounding section header so an empty repository can
// still keep its place in the page hierarchy.
void drawGitActivity(size_t limit) {
    auto& H = g_plmHub;
    for (size_t i = 0; i < H.gitLog.size() && i < limit; ++i) {
        const auto& c = H.gitLog[i];
        ImGui::PushID("git_activity");
        ImGui::PushID(c.hash.c_str());
        plmCardBegin("##git_activity", false, 8.0f, 4.0f, palette.accent, 0.12f);
        plmItemTitle(c.subject);
        dimText("commit %s  \xc2\xb7  %s  \xc2\xb7  %s", c.hash.c_str(),
                c.author.c_str(), c.date.c_str());
        plmCardEnd();
        ImGui::PopID();
        ImGui::PopID();
    }
}

void plmComingSoon(const char* title, const char* note) {
    plmSectionHeader(title);
    plmEmptyState(nullptr, note,
                  "This page is being built out in the Project Hub.");
}

// ---- Overview -----------------------------------------------------------------

void doCheckpoint(AppState& st) {
    auto& H = g_plmHub;
    if (H.busy) return;
    H.busy = true; H.actionErr.clear(); H.actionOk.clear();
    nlohmann::json p = pathParam(st);
    p["message"] = H.checkpointMsg;  // empty is allowed → assist tier may draft
    st.rpc.call("plm_checkpoint", p, [&st](const core::RpcResult& r) {
        auto& H = g_plmHub;
        H.busy = false;
        if (!plmOk(r, H.actionErr)) return;
        H.checkpointMsg[0] = '\0';
        H.actionOk = "Checkpoint saved.";
        st.toast(ui::ToastLevel::Success, "Checkpoint saved");
        plmInvalidate();
    });
}

void doRecordOutside(AppState& st) {
    auto& H = g_plmHub;
    if (H.busy) return;
    H.busy = true; H.actionErr.clear(); H.actionOk.clear();
    st.rpc.call("plm_record_outside", pathParam(st), [&st](const core::RpcResult& r) {
        auto& H = g_plmHub;
        H.busy = false;
        if (!plmOk(r, H.actionErr)) return;
        bool committed = r.result.is_object() && r.result.value("committed", false);
        H.actionOk = committed ? "Recorded outside changes." : "Already up to date.";
        st.toast(ui::ToastLevel::Success, H.actionOk);
        plmInvalidate();
    });
}

}  // namespace

// ---- the one glass action button (declared in plm_hub.h) ----------------------
//
// Deliberately NOT a forward to ui::kit::secondaryButton: that is a themed
// ImGui::Button — flat fill, no atlas icon slot, no frosted base. This is the glass
// language the ribbon and the Parts hub use, and the hub's action rows are built on
// it. What it no longer owns is its own geometry: padding, corner radius and the icon
// tier all come from the kit / metrics, so it tracks the app rhythm.
//
// The Graph tab used to carry a byte-copy of this with padX 12dp instead of 13dp,
// which is why its buttons were a pixel narrower per side than every other page's.
// One definition, published, ends that.
namespace {
constexpr float kGlassAccentIdleA  = 0.66f;
constexpr float kGlassAccentHovA   = 0.80f;
constexpr float kGlassAccentHeldA  = 0.92f;
constexpr float kGlassGhostHovA    = 0.06f;
constexpr float kGlassGhostHeldA   = 0.10f;
constexpr float kGlassBorderA      = 0.55f;   // matches ui::kit's chip/button hairline
}  // namespace

bool plmGlassButton(const char* id, const char* label, const char* icon, bool accent,
                    float widthOverride) {
    const float iconSz  = (icon && icon[0]) ? ui::metrics::U(4.0f) : 0.0f;
    const float iconGap = iconSz > 0.0f ? ui::metrics::U(1.5f) : 0.0f;
    const float padX    = kit::padX();
    const float textW   = (label && label[0]) ? ImGui::CalcTextSize(label).x : 0.0f;
    const float content = iconSz + iconGap + textW;
    const float w = widthOverride > 0.0f ? widthOverride : (padX * 2.0f + content);
    const float h = ImGui::GetFrameHeight();   // aligns with adjacent input rows

    const ImVec2 p0 = ImGui::GetCursorScreenPos();
    const ImVec2 p1(p0.x + w, p0.y + h);
    const bool clicked = ImGui::InvisibleButton(id, ImVec2(w, h));
    const bool hov = ImGui::IsItemHovered(), held = ImGui::IsItemActive();
    ImDrawList* dl = ImGui::GetWindowDrawList();

    // Flat button face + hairline border, the same shadowless surface ui::kit's
    // buttons use. (ui::glassBackground is a PANEL surface — its offset drop shadow
    // made every PLM button look heavier than the rest of the app's controls.)
    const float rndPx = kit::rowRound();
    dl->AddRectFilled(p0, p1, Palette::toU32(palette.button), rndPx);

    ImVec4 fill(palette.accent[0], palette.accent[1], palette.accent[2],
                held ? kGlassAccentHeldA : hov ? kGlassAccentHovA : kGlassAccentIdleA);
    if (accent) {
        dl->AddRectFilled(p0, p1, ImGui::GetColorU32(fill), rndPx);
    } else if (hov) {
        dl->AddRectFilled(p0, p1, ImGui::GetColorU32(ImVec4(
            palette.text[0], palette.text[1], palette.text[2],
            held ? kGlassGhostHeldA : kGlassGhostHovA)), rndPx);
    }
    float edge[4];
    Palette::withAlpha(accent ? palette.accent : palette.border, edge, kGlassBorderA);
    dl->AddRect(p0, p1, Palette::toU32(edge), rndPx, 0, ui::metrics::stroke(1.0f));

    // On the accent wash the ink is derived from that fill, not assumed white — the
    // light theme's accent is bright enough that white text vanishes on it.
    const ImU32 tcol = accent ? ImGui::GetColorU32(ui::maxContrastTextOn(fill))
                              : Palette::toU32(palette.text);
    float x = p0.x + (w - content) * 0.5f;
    if (iconSz > 0.0f) {
        ui::drawIconGlass(dl, icon, ImVec2(x + iconSz * 0.5f, p0.y + h * 0.5f), iconSz,
                          tcol, 0);
        x += iconSz + iconGap;
    }
    if (textW > 0.0f)
        dl->AddText(ImVec2(x, p0.y + (h - ImGui::GetTextLineHeight()) * 0.5f), tcol, label);
    return clicked;
}

void plmDrawOverviewPage(AppState& st) {
    auto& H = g_plmHub;
    const float S = ui::uiScale;
    plmEnsureOverview(st);
    plmEnsureGit(st, false);
    plmEnsurePlanning(st);

    if (!H.ovErr.empty()) { plmError(H.ovErr); return; }

    const auto& ov = H.overview;
    // `overview` may still hold nothing (first visit) or the previous project's answer;
    // cards that read it show a spinner until this project's answer lands, never zeros.
    const bool ovReady = H.ovProject == st.plmProjectPath();
    constexpr size_t kPreviewRows = 3;

    auto bodyCheckpoint = [&]() {
        dimText("Record a point in this project's history you can return to.");
        ImGui::Dummy(ImVec2(0, ui::kit::rowGap()));
        ImGui::SetNextItemWidth(-1);
        bool enter = ImGui::InputTextWithHint("##plm_ckpt", "What changed? (optional)",
            H.checkpointMsg, sizeof(H.checkpointMsg), ImGuiInputTextFlags_EnterReturnsTrue);
        bool save = plmGlassButton("##ckpt_btn", "Save checkpoint", "save", true) || enter;
        if (save && !H.busy) doCheckpoint(st);
        if (H.busy) plmSpinner("Saving");
        plmError(H.actionErr);
        if (!H.actionOk.empty())
            ui::kit::inlineStatus(H.actionOk.c_str(), ui::kit::Tone::Ok);

        if (ovReady && ov.dirtyCount > 0) {
            ui::kit::gapSection();
            plmSectionHeader("Unrecorded changes", "warning_badge");
            char pending[96];
            std::snprintf(pending, sizeof(pending), "%d changed file%s waiting to be recorded.",
                          ov.dirtyCount, ov.dirtyCount == 1 ? " is" : "s are");
            ui::kit::inlineStatus(pending, ui::kit::Tone::Warn);
            const size_t shown = std::min(kPreviewRows, ov.dirtyItems.size());
            for (size_t i = 0; i < shown; ++i) {
                const auto& it = ov.dirtyItems[i];
                plmDocTypeTag(it.docType, it.kind);
                ImGui::SameLine();
                ImGui::AlignTextToFramePadding();
                ImGui::TextWrapped("%s", it.path.c_str());
            }
            if (ov.dirtyItems.size() > shown)
                dimText("%zu more changed file%s", ov.dirtyItems.size() - shown,
                        ov.dirtyItems.size() - shown == 1 ? "" : "s");
            ui::kit::gapRow();
            if (plmGlassButton("##rec_outside", "Record outside changes", "save",
                               true) && !H.busy)
                doRecordOutside(st);
        }
    };

    auto bodyStats = [&]() {
        if (!ovReady) {
            plmSpinner("Loading project overview");
            return;
        }
        struct SC { const char* label; int n; const float* col; };
        const SC scs[] = {
            {"Files",      ov.itemsCount,     palette.accent},
            {"Unsaved",    ov.dirtyCount,     ov.dirtyCount ? palette.statusWarn : palette.textDim},
            {"Tasks",      ov.openTasks,      palette.textDim},
            {"Issues",     ov.openIssues,     ov.openIssues ? palette.statusWarn : palette.textDim},
            {"Milestones", ov.openMilestones, palette.textDim},
        };
        const float cw = 86 * S;
        const float cg = ui::kit::rowGap();
        int perRow = std::max(1, (int)std::floor(
            (ImGui::GetContentRegionAvail().x + cg) / (cw + cg)));
        for (int i = 0; i < (int)IM_ARRAYSIZE(scs); ++i) {
            if (i % perRow) ImGui::SameLine(0, cg);
            statCard(scs[i].label, scs[i].n, scs[i].col, cw);
        }
        if (!ov.nextMilestone.empty()) {
            ui::kit::gapRow();
            dimText("Next milestone: %s", ov.nextMilestone.c_str());
        }
    };

    auto bodyCheckpointActivity = [&]() {
        plmSectionHeader("Checkpoint activity", "history");
        if (!ovReady) {
            plmSpinner("Loading checkpoints");
            return;
        }
        if (ov.recent.empty()) {
            (void)ui::kit::emptyState(
                "history", "No checkpoints yet",
                "Save a checkpoint above to create a restorable project version.");
            return;
        }
        const size_t shown = std::min(kPreviewRows, ov.recent.size());
        for (size_t i = 0; i < shown; ++i) {
            const auto& e = ov.recent[i];
            ImGui::PushID(e.commit.c_str());
            plmCardBegin("##checkpoint_activity", false, 8.0f, 3.0f,
                         e.kind == "release" ? palette.statusOk : palette.accent, 0.15f);
            const std::string headline = !e.message.empty() ? e.message
                                       : (!e.summary.empty() ? e.summary : "(saved)");
            const char* entryLabel = "Checkpoint";
            ui::kit::Tone entryTone = ui::kit::Tone::Accent;
            if (e.kind == "release") {
                entryLabel = "Release";
                entryTone = ui::kit::Tone::Ok;
            } else if (e.kind == "auto") {
                entryLabel = "Auto-save";
                entryTone = ui::kit::Tone::Neutral;
            } else if (e.kind == "restore") {
                entryLabel = "Restore";
                entryTone = ui::kit::Tone::Info;
            }
            ui::kit::chip(entryLabel, entryTone);
            ImGui::SameLine();
            plmItemTitle(headline, nullptr, /*alignFrame=*/true);
            dimText("%s  \xc2\xb7  %s", e.author.c_str(), plmNiceTime(e.authoredAt).c_str());
            plmCardEnd();
            ImGui::PopID();
        }
        if (ov.recent.size() > shown)
            dimText("%zu more checkpoint entries in History", ov.recent.size() - shown);
    };

    auto bodyGitHistory = [&]() {
        plmSectionHeader("Git history", "git");
        if (!H.gitErr.empty()) {
            plmError(H.gitErr);
            return;
        }
        if (H.gitLoading && H.gitLog.empty()) {
            plmSpinner("Loading Git history");
            return;
        }
        if (H.gitMissing) {
            (void)ui::kit::blockedState(
                "Git unavailable", "Git is not installed on this machine.",
                "Install Git to show repository history for this project.");
            return;
        }
        if (!H.gitIsRepo) {
            (void)ui::kit::emptyState(
                "git", "No Git history",
                "This project folder is not a Git repository. Checkpoint history remains available.");
            return;
        }
        if (H.gitLog.empty()) {
            (void)ui::kit::emptyState(
                "git", "No Git commits yet",
                "Commits made in this repository will appear here.");
            return;
        }
        drawGitActivity(kPreviewRows);
        if (H.gitLog.size() > kPreviewRows)
            dimText("%zu more commits in Git", H.gitLog.size() - kPreviewRows);
    };

    auto bodyTasks = [&]() {
        if (!ovReady) {
            plmSpinner("Loading tasks");
            return;
        }
        if (ov.tasks.empty()) {
            (void)ui::kit::emptyState(
                "prompt_edit", "No open tasks",
                "Create and assign work from the Planning page.");
            return;
        }
        const size_t shown = std::min(kPreviewRows, ov.tasks.size());
        for (size_t i = 0; i < shown; ++i) {
            const auto& t = ov.tasks[i];
            ImGui::PushID(t.id.c_str());
            plmCardBegin("##overview_task", false, 8.0f, 6.0f,
                         plmTintPriority(t.priority), 0.32f);
            plmItemTitle(t.title);
            priorityDot(t.priority);
            if (!t.assignee.empty()) dimText("@%s", t.assignee.c_str());
            plmCardEnd();
            ImGui::PopID();
        }
        if (ov.tasks.size() > shown)
            dimText("%zu more open task%s in Planning", ov.tasks.size() - shown,
                    ov.tasks.size() - shown == 1 ? "" : "s");
    };

    auto bodyIssues = [&]() {
        const bool planFailed = !H.planErr.empty();
        if (planFailed) plmError(H.planErr);

        size_t openCount = 0;
        for (const auto& issue : H.issues)
            if (issue.state != "closed") ++openCount;
        if (H.planLoading && H.issues.empty()) {
            plmSpinner("Loading issues");
            return;
        }
        // Counts and the issue list are separate answers; spin only while the list can
        // still change, never forever on a count the finished list disagrees with.
        if (openCount == 0 && ovReady && ov.openIssues > 0 &&
            (H.planLoading || H.planGen != H.generation)) {
            plmSpinner("Loading issue details");
            return;
        }
        if (openCount == 0) {
            if (planFailed) return;
            (void)ui::kit::emptyState(
                "issue_bug", "No open issues",
                "Log defects and follow them to closure from the Planning page.");
            return;
        }
        size_t shown = 0;
        for (const auto& issue : H.issues) {
            if (shown == kPreviewRows) break;
            if (issue.state == "closed") continue;
            ImGui::PushID(issue.id.c_str());
            plmCardBegin("##overview_issue", false, 8.0f, 6.0f,
                         plmTintSeverity(issue.severity), 0.32f);
            severityBadge(issue.severity);
            ImGui::SameLine();
            plmItemTitle(issue.title, nullptr, /*alignFrame=*/true);
            if (!issue.assignee.empty()) dimText("@%s", issue.assignee.c_str());
            plmCardEnd();
            ImGui::PopID();
            ++shown;
        }
        if (openCount > shown)
            dimText("%zu more open issue%s in Planning", openCount - shown,
                    openCount - shown == 1 ? "" : "s");
    };

    const float availableW = ImGui::GetContentRegionAvail().x;
    const float panelGap = ui::kit::gutter();
    const bool twoColumns = availableW >= ui::metrics::px(760.0f);
    const float panelW = twoColumns ? (availableW - panelGap) * 0.5f : 0.0f;

    // Primary actions and project health stay first; on a narrow pane they stack
    // instead of squeezing either panel below a useful reading width.
    plmPanelBegin("##ov_ckpt", "Save a checkpoint", "save", panelW);
    bodyCheckpoint();
    plmPanelEnd();
    if (twoColumns) ImGui::SameLine(0, panelGap);
    else ui::kit::gapRow();
    plmPanelBegin("##ov_stats", "Project overview", "app", panelW);
    bodyStats();
    plmPanelEnd();

    ui::kit::gapRow();

    // One activity region, two truthful timelines. The inner section headers keep
    // PLM checkpoints distinct from raw repository commits without another card tier.
    plmPanelBegin("##ov_recent", "Recent activity", "history");
    if (twoColumns &&
        ImGui::BeginTable("##overview_activity_columns", 2,
                          ImGuiTableFlags_SizingStretchSame |
                          ImGuiTableFlags_BordersInnerV |
                          ImGuiTableFlags_NoPadOuterX)) {
        ImGui::TableNextColumn();
        bodyCheckpointActivity();
        ImGui::TableNextColumn();
        bodyGitHistory();
        ImGui::EndTable();
    } else {
        bodyCheckpointActivity();
        ui::kit::gapSection();
        ui::kit::rule();
        ui::kit::gapSection();
        bodyGitHistory();
    }
    plmPanelEnd();

    ui::kit::gapRow();

    plmPanelBegin("##ov_tasks", "Open tasks", "prompt_edit", panelW);
    bodyTasks();
    plmPanelEnd();
    if (twoColumns) ImGui::SameLine(0, panelGap);
    else ui::kit::gapRow();
    plmPanelBegin("##ov_issues", "Open issues", "issue_bug", panelW);
    bodyIssues();
    plmPanelEnd();
}

// ---- Files --------------------------------------------------------------------

namespace {

struct KindFilter { const char* label; const char* match; };  // match "" = all
static const KindFilter kFilters[] = {
    {"All", ""}, {"Design", "design"}, {"Documents", "docs"}, {"Code", "code"},
};

bool passesFilter(const PlmItemRow& it, int f) {
    if (f == 0) return true;
    if (f == 1) return it.kind == "part" || it.kind == "assembly" ||
                       it.kind == "drawing" || it.kind == "ecad" || it.kind == "cam";
    if (f == 2) return it.kind == "requirements" ||
                       (it.kind == "deliverable" && it.docType != "software" &&
                        it.docType != "linked_repo");
    if (f == 3) return it.docType == "software" || it.docType == "linked_repo";
    return true;
}

void doTrack(AppState& st) {
    auto& H = g_plmHub;
    if (H.busy || !H.trackPath[0]) return;
    H.busy = true; H.actionErr.clear();
    nlohmann::json p = pathParam(st);
    p["file_path"] = H.trackPath;
    st.rpc.call("plm_item_add", p, [&st](const core::RpcResult& r) {
        auto& H = g_plmHub;
        H.busy = false;
        if (!plmOk(r, H.actionErr)) { st.toast(ui::ToastLevel::Error, H.actionErr); return; }
        H.trackPath[0] = '\0';
        st.toast(ui::ToastLevel::Success, "Tracking new file");
        plmInvalidate();
    });
}

void doLink(AppState& st) {
    auto& H = g_plmHub;
    if (H.busy || !H.linkUrl[0]) return;
    H.busy = true; H.actionErr.clear();
    nlohmann::json p = pathParam(st);
    p["url"] = H.linkUrl;
    p["name"] = H.linkName[0] ? H.linkName : "Linked repo";
    if (H.linkCheckout[0]) p["checkout_path"] = H.linkCheckout;  // local clone for pinning
    st.rpc.call("plm_link_repo", p, [&st](const core::RpcResult& r) {
        auto& H = g_plmHub;
        H.busy = false;
        if (!plmOk(r, H.actionErr)) { st.toast(ui::ToastLevel::Error, H.actionErr); return; }
        H.linkUrl[0] = '\0'; H.linkName[0] = '\0'; H.linkCheckout[0] = '\0';
        st.toast(ui::ToastLevel::Success, "Linked GitHub repo");
        plmInvalidate();
    });
}

// Take an absolute path and store it in `dst`, made relative to the project folder
// (plm_item_add expects a project-relative path). A pick outside the project is
// rejected with a toast. Empty `abs` (dialog cancelled) is a silent no-op.
void storeProjectRelative(AppState& st, std::string abs, char* dst, size_t dstSz) {
    if (abs.empty()) return;
    std::string root = st.plmProjectPath();
    for (char& c : abs)  if (c == '\\') c = '/';
    for (char& c : root) if (c == '\\') c = '/';
    if (!root.empty() && root.back() == '/') root.pop_back();
    if (abs.size() > root.size() + 1 &&
        abs.compare(0, root.size(), root) == 0 && abs[root.size()] == '/') {
        std::snprintf(dst, dstSz, "%s", abs.c_str() + root.size() + 1);
    } else {
        st.toast(ui::ToastLevel::Error, "Pick a file or folder inside the project");
    }
}

void browseTrackFile(AppState& st) {
    storeProjectRelative(st, ui::openFileDialog(
        "All files (*.*)\0*.*\0Documents (*.pdf *.docx *.xlsx)\0*.pdf;*.docx;*.xlsx\0"),
        g_plmHub.trackPath, sizeof(g_plmHub.trackPath));
}

void browseTrackFolder(AppState& st) {
    storeProjectRelative(st, ui::openFolderDialog(),
                         g_plmHub.trackPath, sizeof(g_plmHub.trackPath));
}

// The linked-repo checkout is an arbitrary local folder (not project-relative) —
// store it verbatim.
void browseCheckoutFolder(AppState&) {
    std::string abs = ui::openFolderDialog();
    if (!abs.empty())
        std::snprintf(g_plmHub.linkCheckout, sizeof(g_plmHub.linkCheckout), "%s", abs.c_str());
}

void doUntrack(AppState& st, const std::string& itemId) {
    auto& H = g_plmHub;
    if (H.busy) return;
    H.busy = true;
    nlohmann::json p = pathParam(st);
    p["item_id"] = itemId;
    st.rpc.call("plm_item_untrack", p, [&st](const core::RpcResult& r) {
        auto& H = g_plmHub;
        H.busy = false;
        std::string h; if (!plmOk(r, h)) { st.toast(ui::ToastLevel::Error, h); return; }
        st.toast(ui::ToastLevel::Info, "Stopped tracking");
        plmInvalidate();
    });
}


// The where-used explorer: a cached band under the Files list answering "who uses
// this item?" with the DIRECT consumers plm_where_used derives from the project's
// own documents — nothing inferred, no invented relationships. Rows are drillable
// (ask the same question of a consumer) and openable when they carry a real path.
void drawWhereUsedExplorer(AppState& st, float bandH) {
    auto& H = g_plmHub;
    const float S = ui::uiScale;
    plmEnsureWhereUsed(st);

    // Selected item's display title (fall back to the raw id if its row is gone).
    std::string title = H.wuTarget;
    for (const auto& it : H.items)
        if (it.itemId == H.wuTarget) { title = it.path; break; }

    ui::pushGlassChildStyle(9.0f);
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(10 * S, 8 * S));
    ImGui::BeginChild("##wu_panel", ImVec2(0, bandH), ImGuiChildFlags_Borders);
    ImGui::PopStyleVar();

    plmSectionHeader("Where used", "mesh");
    ImGui::SameLine();
    ImGui::AlignTextToFramePadding();
    dimText("%s", title.c_str());
    const float closeW = 52 * S;
    ImGui::SameLine(ImGui::GetContentRegionMax().x - closeW);
    const bool close = ImGui::SmallButton("Close");
    ImGui::Separator();

    if (H.wuLoading && (H.wuFor != H.wuTarget || H.wuRows.empty())) {
        plmSpinner("Finding consumers");
    } else if (!H.wuErr.empty()) {
        plmError(H.wuErr);
    } else if (H.wuRows.empty()) {
        (void)ui::kit::emptyState("mesh", "Not used anywhere else",
            "No assembly, drawing or CAM job in this project references it.");
    } else {
        ImGui::BeginChild("##wu_rows", ImVec2(0, 0), ImGuiChildFlags_None);
        for (const auto& c : H.wuRows) {
            ImGui::PushID(c.itemId.c_str());
            plmCardBegin("##wu_row", false, 8.0f, 4.0f, palette.accent, 0.12f);
            plmDocTypeTag("", c.kind);
            ImGui::SameLine();
            ImGui::AlignTextToFramePadding();
            ImGui::TextWrapped("%s", !c.path.empty() ? c.path.c_str()
                                                     : c.itemId.c_str());
            ImGui::SameLine();
            char via[32];
            if (c.via == "source")
                std::snprintf(via, sizeof(via), "source");
            else if (c.count > 1)
                std::snprintf(via, sizeof(via), "component x%d", c.count);
            else
                std::snprintf(via, sizeof(via), "component");
            ui::kit::chip(via, ui::kit::Tone::Neutral);
            if (!c.partNumber.empty()) dimText("%s", c.partNumber.c_str());
            plmActionRowBegin();
            if (ImGui::SmallButton("Where used")) {   // drill one level upward
                H.wuTarget = c.itemId;
                H.wuErr.clear();
            }
            if (!c.path.empty()) {
                ImGui::SameLine();
                if (ImGui::SmallButton("Open")) {
                    std::string abs = st.plmProjectPath();
                    if (!abs.empty() && abs.back() != '/' && abs.back() != '\\')
                        abs += '/';
                    abs += c.path;
                    for (char& ch : abs) if (ch == '\\') ch = '/';
                    st.requestOpenPath = abs;
                }
            }
            plmActionRowEnd();
            plmCardEnd();
            ImGui::PopID();
        }
        ImGui::EndChild();
    }
    ImGui::EndChild();
    ui::popGlassChildStyle();
    if (close) H.wuTarget.clear();
}
}  // namespace

void drawFilesTab(AppState& st) {
    auto& H = g_plmHub;
    const float S = ui::uiScale;
    plmEnsureItems(st);

    // track-a-file row: pick a file/folder via the native picker (no typing).
    dimText("Add a document or folder from this project to track its history.");
    ImGui::Spacing();
    if (ImGui::Button("Browse file\xe2\x80\xa6")) browseTrackFile(st);
    ImGui::SameLine();
    if (ImGui::Button("Browse folder\xe2\x80\xa6")) browseTrackFolder(st);
    if (H.trackPath[0]) {
        ImGui::SameLine();
        accentButtonColors();
        if (ImGui::Button("Track", ImVec2(70 * S, 0))) doTrack(st);
        ImGui::PopStyleColor(2);
        ImGui::SameLine();
        if (ImGui::Button("Clear")) H.trackPath[0] = '\0';
        dimText("Selected: %s", H.trackPath);
    }

    // link a GitHub / Git repo (kept under version control externally; its issues
    // are tracked here in the project). Optional local checkout enables pinning.
    ImGui::Dummy(ImVec2(0, 6 * S));
    dimText("Link a GitHub / Git repo (version-controlled outside Chudware).");
    ImGui::Spacing();
    ImGui::PushStyleVar(ImGuiStyleVar_FramePadding, ImVec2(6 * S, 4 * S));
    ImGui::SetNextItemWidth(-1);
    ImGui::InputTextWithHint("##linkurl", "https://github.com/owner/repo",
                             H.linkUrl, sizeof(H.linkUrl));
    ImGui::SetNextItemWidth(200 * S);
    ImGui::InputTextWithHint("##linkname", "display name (optional)",
                             H.linkName, sizeof(H.linkName));
    ImGui::PopStyleVar();
    ImGui::SameLine();
    if (ImGui::Button("Checkout folder\xe2\x80\xa6")) browseCheckoutFolder(st);
    if (H.linkCheckout[0]) { ImGui::SameLine(); dimText("%s", H.linkCheckout); }
    ImGui::Spacing();
    accentButtonColors();
    if (ImGui::Button("Link repo", ImVec2(110 * S, 0))) doLink(st);
    ImGui::PopStyleColor(2);
    plmError(H.actionErr);

    // filter pills + sort dropdown
    ImGui::Dummy(ImVec2(0, 6 * S));
    for (int i = 0; i < (int)(sizeof(kFilters) / sizeof(kFilters[0])); ++i) {
        if (i) ImGui::SameLine();
        bool sel = (H.kindFilter == i);
        ImGui::PushStyleColor(ImGuiCol_Button, sel
            ? ImVec4(palette.accent[0], palette.accent[1], palette.accent[2], 0.30f)
            : ImVec4(palette.button[0], palette.button[1], palette.button[2], 0.5f));
        if (ImGui::SmallButton(kFilters[i].label)) H.kindFilter = i;
        ImGui::PopStyleColor();
    }
    // Sort control, pinned to the right of the filter row.
    static const char* kSort[] = {
        "Name (A-Z)", "Type", "Revision", "Status", "Unsaved first",
    };
    ui::kit::fieldEnum("##sort", "Sort", &H.sortMode, kSort, IM_ARRAYSIZE(kSort),
                      ui::kit::FieldOpts().withEnumStyle(ui::kit::EnumStyle::Combo));
    ImGui::Separator();
    ImGui::Spacing();

    // Only the item list scrolls; the header + track/link + filter row above stay
    // pinned at the top of the panel. An open where-used explorer takes a band at
    // the bottom; the list gives up that height.
    const float wuBandH = !H.wuTarget.empty()
        ? std::max(150.0f * S, ImGui::GetContentRegionAvail().y * 0.38f) : 0.0f;
    auto endList = [&]() {
        ImGui::EndChild();
        if (wuBandH > 0.0f) drawWhereUsedExplorer(st, wuBandH);
    };
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(0, 0));
    ImGui::BeginChild("##files_list",
                      ImVec2(0, wuBandH > 0.0f ? -(wuBandH + 6 * S) : 0.0f),
                      ImGuiChildFlags_None);
    ImGui::PopStyleVar();
    if (H.itemsLoading && H.items.empty()) { plmSpinner("Loading"); endList(); return; }
    if (!H.itemsErr.empty()) { plmError(H.itemsErr); endList(); return; }

    // Rebuild only when rows or controls change, not on every rendered frame.
    if (H.filesViewRevision != H.itemsRevision ||
        H.filesViewFilter != H.kindFilter || H.filesViewSort != H.sortMode) {
        auto& view = H.filesView;
        view.clear();
        view.reserve(H.items.size());
        for (size_t i = 0; i < H.items.size(); ++i)
            if (passesFilter(H.items[i], H.kindFilter)) view.push_back(i);
        std::stable_sort(view.begin(), view.end(), [&](size_t ai, size_t bi) {
            const auto& a = H.items[ai];
            const auto& b = H.items[bi];
            switch (H.sortMode) {
                case 1: return a.kind + a.docType < b.kind + b.docType;
                case 2: {  // revision, highest first
                    const std::string& ra = !a.rev.empty() ? a.rev : a.workingRev;
                    const std::string& rb = !b.rev.empty() ? b.rev : b.workingRev;
                    return ra > rb;
                }
                case 3: return a.lifecycle < b.lifecycle;
                case 4: return a.dirty > b.dirty;
                default: return a.path < b.path;
            }
        });
        H.filesViewRevision = H.itemsRevision;
        H.filesViewFilter = H.kindFilter;
        H.filesViewSort = H.sortMode;
    }

    int shown = 0;
    for (size_t index : H.filesView) {
        const auto& it = H.items[index];
        ++shown;
        ImGui::PushID(it.itemId.c_str());
        bool sel = (H.selectedItem == it.itemId);
        const float* ftint = it.dirty ? palette.statusWarn
                           : (it.lifecycle == "Released" ? palette.statusOk : palette.accent);
        plmCardBegin("##fitem", sel, 8.0f, 4.0f, ftint, 0.13f);

        // one padded row: [TAG] path .......... rev · lifecycle  ●
        // The badge is measured (same text plmLifecycleBadge prints) and pinned right; the
        // path is elided to the space left of it so a long path never runs under the badge.
        const std::string& badgeRev = !it.rev.empty() ? it.rev : it.workingRev;
        const std::string badgeText =
            badgeRev.empty() ? it.lifecycle : badgeRev + " \xc2\xb7 " + it.lifecycle;
        const float dotW = it.dirty ? ImGui::GetStyle().ItemSpacing.x + 8.0f * S : 0.0f;
        const float badgeW = ImGui::CalcTextSize(badgeText.c_str()).x + dotW;
        const float badgeGap = 12.0f * S;
        plmDocTypeTag(it.docType, it.kind);
        ImGui::SameLine();
        ImGui::AlignTextToFramePadding();
        const float pathX = ImGui::GetCursorPosX();
        const float rightX = ImGui::GetContentRegionMax().x;
        const float pinX = rightX - badgeW;
        // Below a readable path width the badge drops to its own line instead.
        const bool badgeInline = pinX - badgeGap - pathX >= 96.0f * S;
        const float pathMaxW = badgeInline ? pinX - badgeGap - pathX : rightX - pathX;
        const std::string pathShown = ui::kit::elide(it.path.c_str(), pathMaxW);
        ImGui::TextUnformatted(pathShown.c_str());
        if (pathShown != it.path) ui::kit::itemTooltip(it.path.c_str());

        if (badgeInline) ImGui::SameLine(pinX);
        plmLifecycleBadge(it.lifecycle, badgeRev);
        if (it.dirty) {
            // filled dot via the draw-list — the ● glyph isn't in the font atlas.
            ImGui::SameLine();
            const float r = 4.0f * S;
            ImVec2 o = ImGui::GetCursorScreenPos();
            ImGui::Dummy(ImVec2(r * 2, ImGui::GetTextLineHeight()));
            ImGui::GetWindowDrawList()->AddCircleFilled(
                ImVec2(o.x + r, o.y + ImGui::GetTextLineHeight() * 0.5f), r,
                ImGui::GetColorU32(ImVec4(palette.statusWarn[0], palette.statusWarn[1],
                                          palette.statusWarn[2], 1.0f)), 16);
            if (ImGui::IsItemHovered()) ImGui::SetTooltip("unsaved changes");
        }

        if (sel) {
            ImGui::Spacing();
            dimText("%s  \xc2\xb7  %s%s", it.kind.c_str(),
                    it.docType.empty() ? "design document" : it.docType.c_str(),
                    it.partNumber.empty() ? "" : ("  \xc2\xb7  " + it.partNumber).c_str());
            if (ImGui::SmallButton("Where used")) {
                H.wuTarget = it.itemId;   // opens the cached explorer band below
                H.wuErr.clear();
            }
            ImGui::SameLine();
            if (ImGui::SmallButton("Open")) {
                std::string abs = st.plmProjectPath();
                if (!abs.empty() && abs.back() != '/' && abs.back() != '\\')
                    abs += '/';
                abs += it.path;
                for (char& c : abs) if (c == '\\') c = '/';
                st.requestOpenPath = abs;
            }
            ImGui::SameLine();
            ImGui::PushStyleColor(ImGuiCol_Button,
                ImVec4(palette.statusErr[0], palette.statusErr[1], palette.statusErr[2], 0.55f));
            if (ImGui::SmallButton("Stop tracking")) doUntrack(st, it.itemId);
            ImGui::PopStyleColor();
        }
        if (plmCardEnd()) H.selectedItem = sel ? "" : it.itemId;
        ImGui::PopID();
    }
    if (shown == 0)
        plmEmptyState("(#)", "No files here yet",
                      "Save a design, or Track a document/folder above.");
    endList();
}

// The Files destination folds in History + Releases as sub-tabs, so the whole
// "what's in the project / how it changed / what's released" story lives together.
void plmDrawFilesPage(AppState& st) {
    const float S = ui::uiScale;
    const float gap = 10.0f * S;

    // A bordered, padded panel so its content sits inset from the edges instead of
    // clipping flush-left. AutoResize is off (fixed-size split panes).
    auto panelBegin = [&](const char* id, ImVec2 sz) {
        ImGui::PushStyleColor(ImGuiCol_ChildBg,
            ImVec4(palette.text[0], palette.text[1], palette.text[2], 0.028f));
        ImGui::PushStyleColor(ImGuiCol_Border,
            ImVec4(palette.text[0], palette.text[1], palette.text[2], 0.12f));
        ImGui::PushStyleVar(ImGuiStyleVar_ChildRounding, 6.0f * S);
        ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(12 * S, 10 * S));
        ImGui::BeginChild(id, sz, ImGuiChildFlags_Borders);
    };
    auto panelEnd = [&]() {
        ImGui::EndChild();
        ImGui::PopStyleVar(2);
        ImGui::PopStyleColor(2);
    };

    // Files span the full left vertical side; Releases (top) + History (bottom)
    // share the right side. No tab bar — everything visible at once.
    const float leftW = ImGui::GetContentRegionAvail().x * 0.50f;

    panelBegin("##files_left", ImVec2(leftW, 0));
    plmSectionHeader("Files", "body_folder");
    ImGui::Spacing();
    drawFilesTab(st);
    panelEnd();

    ImGui::SameLine(0.0f, gap);

    ImGui::BeginChild("##files_right", ImVec2(0, 0), ImGuiChildFlags_None);
    const float rightH = ImGui::GetContentRegionAvail().y;
    const float topH = rightH * 0.5f - gap * 0.5f;

    panelBegin("##files_releases", ImVec2(0, topH));
    plmDrawReleasesPage(st);
    panelEnd();

    ImGui::Dummy(ImVec2(0, gap));

    panelBegin("##files_history", ImVec2(0, 0));
    plmDrawHistoryPage(st);
    panelEnd();
    ImGui::EndChild();
}

// ---- History ------------------------------------------------------------------

void plmDrawHistoryPage(AppState& st) {
    auto& H = g_plmHub;
    const float S = ui::uiScale;
    plmEnsureHistory(st);
    plmEnsureItems(st);   // the item filter draws its choices from the Files rows

    plmSectionHeader("History", "history");

    // scope toggle
    static const char* kScope[] = {"Design", "Planning", "All"};
    if (ui::kit::segmented("##hist_scope", kScope, 3, &H.histScope))
        H.histGen = -1;
    // Live filters: item narrows the fetch itself (plm_history filters by item_id
    // server-side); person and AI-vs-human apply to the cached rows. Every choice
    // is derived from cached rows — nothing invented.
    {
        static unsigned long long itemRevision = ~0ULL, authorRevision = ~0ULL;
        static std::vector<std::string> itemNames, authorNames;
        if (itemRevision != H.itemsRevision) {
            itemRevision = H.itemsRevision;
            itemNames.assign(1, "All items");
            itemNames.reserve(H.items.size() + 1);
            for (const auto& item : H.items) itemNames.push_back(item.path);
        }
        int selectedItem = 0;
        for (size_t i = 0; i < H.items.size(); ++i)
            if (H.items[i].itemId == H.histItemFilter) { selectedItem = (int)i + 1; break; }
        if (ui::kit::fieldCombo("##hist_item", "Item", &selectedItem, itemNames)) {
            H.histItemFilter = selectedItem > 0 ? H.items[(size_t)selectedItem - 1].itemId : "";
            H.histGen = -1;
        }
        if (authorRevision != H.historyRevision) {
            authorRevision = H.historyRevision;
            std::set<std::string> authors;
            for (const auto& entry : H.history)
                if (!entry.author.empty()) authors.insert(entry.author);
            authorNames.assign(1, "Anyone");
            authorNames.insert(authorNames.end(), authors.begin(), authors.end());
        }
        const auto author = std::find(authorNames.begin() + 1, authorNames.end(), H.histAuthorFilter);
        int selectedAuthor = author == authorNames.end() ? 0 : (int)(author - authorNames.begin());
        if (ui::kit::fieldCombo("##hist_author", "Author", &selectedAuthor, authorNames))
            H.histAuthorFilter = selectedAuthor > 0 ? authorNames[(size_t)selectedAuthor] : "";
        static const char* kActor[] = {"All", "People", "AI"};
        const float actorW = 3.0f * (ImGui::CalcTextSize("People").x + 2.0f * ui::kit::padX());
        (void)ui::kit::segmented("##hist_actor", kActor, 3, &H.histActor, actorW);
    }
    ImGui::Separator();
    ImGui::Spacing();

    // Header + scope toggle stay pinned; the timeline scrolls below.
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(0, 0));
    ImGui::BeginChild("##hist_scroll", ImVec2(0, 0), ImGuiChildFlags_None);
    ImGui::PopStyleVar();
    if (!H.histErr.empty()) { plmError(H.histErr); ImGui::EndChild(); return; }
    if (H.histLoading && H.history.empty()) { plmSpinner("Loading"); ImGui::EndChild(); return; }
    if (H.history.empty()) {
        plmEmptyState("[~]", "No history yet", "Save a checkpoint to start tracking.");
        ImGui::EndChild();
        return;
    }

    ImDrawList* dl = ImGui::GetWindowDrawList();
    const float lineX = ImGui::GetCursorScreenPos().x + 9 * S;
    ImU32 lineCol = ImGui::ColorConvertFloat4ToU32(
        ImVec4(palette.border[0], palette.border[1], palette.border[2], palette.border[3]));
    float prevY = -1.0f;
    // client-side row filters (person, AI-vs-human) over the cached fetch
    auto passes = [&](const PlmEntryRow& e) {
        if (!H.histAuthorFilter.empty() && e.author != H.histAuthorFilter)
            return false;
        if (H.histActor == 1 && e.authorKind == "ai") return false;
        if (H.histActor == 2 && e.authorKind != "ai") return false;
        return true;
    };
    size_t matched = 0;
    static std::set<std::string> expandedNoChangeRuns;
    auto noChange = [](const PlmEntryRow& entry) {
        return entry.scope != "planning" && entry.kind != "release" &&
            entry.summary == "No design changes." &&
            (entry.message.empty() || entry.message == entry.summary);
    };
    size_t expandedUntil = 0;
    for (size_t index = 0; index < H.history.size(); ++index) {
        const auto& e = H.history[index];
        if (!passes(e)) continue;
        if (index >= expandedUntil && noChange(e)) {
            size_t end = index + 1;
            while (end < H.history.size() && noChange(H.history[end]) &&
                   passes(H.history[end])) ++end;
            if (end - index > 1) {
                const std::string key = st.plmProjectPath() + ":" + e.commit;
                const bool open = expandedNoChangeRuns.count(key) != 0;
                ImGui::PushID(e.commit.c_str());
                const std::string label = (open ? "Hide " : "Show ") +
                    std::to_string(end - index) + " checkpoints · No design changes";
                if (ui::kit::secondaryButton(label.c_str())) {
                    if (open) expandedNoChangeRuns.erase(key);
                    else expandedNoChangeRuns.insert(key);
                }
                ImGui::PopID();
                if (!open) {
                    matched += end - index;
                    index = end - 1;
                    continue;
                }
                expandedUntil = end;
            }
        }
        ++matched;
        ImGui::PushID(e.commit.c_str());
        bool sel = (H.selectedEntry == e.commit);
        bool rel = (e.kind == "release");

        ImGui::Indent(26 * S);
        float cardTop = ImGui::GetCursorScreenPos().y;
        const float* htint = rel ? palette.statusOk
                           : (e.scope == "planning" ? palette.statusWarn : palette.accent);
        plmCardBegin("##hrow", sel, 8.0f, 4.0f, htint, 0.13f);
        if (rel) {
            ImGui::PushStyleColor(ImGuiCol_Text,
                ImVec4(palette.statusOk[0], palette.statusOk[1], palette.statusOk[2], 1.0f));
            ImGui::AlignTextToFramePadding();
            ImGui::TextUnformatted("[Release]");
            ImGui::PopStyleColor();
            ImGui::SameLine();
        }
        else if (e.kind == "auto") {
            ImGui::PushStyleColor(ImGuiCol_Text,
                ImVec4(palette.textDim[0], palette.textDim[1], palette.textDim[2], 0.85f));
            ImGui::AlignTextToFramePadding();
            ImGui::TextUnformatted("save");
            ImGui::PopStyleColor();
            ImGui::SameLine();
        }
        std::string headline = !e.message.empty() ? e.message
                             : (!e.summary.empty() ? e.summary : "(saved)");
        plmItemTitle(headline, nullptr, /*alignFrame=*/rel || e.kind == "auto");
        dimText("%s  \xc2\xb7  %s", e.author.c_str(), plmNiceTime(e.authoredAt).c_str());
        if (sel) {
            ImGui::PushStyleColor(ImGuiCol_Button,
                ImVec4(palette.button[0], palette.button[1], palette.button[2], 0.8f));
            if (!H.busy && ImGui::SmallButton("Restore this version")) {
                H.busy = true;
                nlohmann::json p = pathParam(st);
                p["commit"] = e.commit;
                st.rpc.call("plm_restore", p, [&st](const core::RpcResult& r) {
                    g_plmHub.busy = false;
                    std::string h; if (!plmOk(r, h)) { st.toast(ui::ToastLevel::Error, h); return; }
                    st.toast(ui::ToastLevel::Success, "Restored — saved as the latest version");
                    plmInvalidate();
                });
            }
            ImGui::PopStyleColor();
        }
        bool clicked = plmCardEnd();
        ImGui::Unindent(26 * S);

        // timeline node + connector, centred on the card we just drew. plmCardEnd
        // appends a 4px gap Dummy, so the card bottom is the cursor minus that gap.
        float cardBot = ImGui::GetCursorScreenPos().y - 4 * S;
        float midY = (cardTop + cardBot) * 0.5f;
        if (prevY >= 0) dl->AddLine(ImVec2(lineX, prevY), ImVec2(lineX, midY), lineCol, 2 * S);
        ImU32 dotCol = ImGui::ColorConvertFloat4ToU32(rel
            ? ImVec4(palette.statusOk[0], palette.statusOk[1], palette.statusOk[2], 1.0f)
            : ImVec4(palette.accent[0], palette.accent[1], palette.accent[2], 0.85f));
        dl->AddCircleFilled(ImVec2(lineX, midY), 5 * S, dotCol);
        prevY = midY;

        if (clicked) H.selectedEntry = sel ? "" : e.commit;
        ImGui::PopID();
    }
    if (matched == 0)
        plmEmptyState("history", "No matching entries",
                      "Nothing in this scope matches the item, person and AI "
                      "filters. Loosen a filter to see more history.");
    ImGui::EndChild();
}

// ---- Releases -----------------------------------------------------------------

namespace {
static std::string s_releaseItem;   // item whose release form is open
static char        s_releaseNote[256] = {};

void doRelease(AppState& st, const std::string& itemId, bool confirm) {
    auto& H = g_plmHub;
    if (H.busy) return;
    H.busy = true; H.actionErr.clear();
    nlohmann::json p = pathParam(st);
    p["item_id"] = itemId;
    p["message"] = s_releaseNote;
    p["confirm"] = confirm;
    st.rpc.call("plm_release", p, [&st](const core::RpcResult& r) {
        auto& H = g_plmHub;
        H.busy = false;
        if (!plmOk(r, H.actionErr)) return;
        std::string rev = r.result.value("rev", "");
        st.toast(ui::ToastLevel::Success, "Released revision " + rev);
        s_releaseItem.clear(); s_releaseNote[0] = '\0';
        plmInvalidate();
    });
}
}  // namespace


void plmDrawReleasesPage(AppState& st) {
    auto& H = g_plmHub;
    const float S = ui::uiScale;
    plmEnsureItems(st);
    plmEnsureGit(st, false); // releases consume status/history, not the Git browser

    plmSectionHeader("Releases", "ok_badge");
    dimText("Stamp the next revision and freeze the design.");
    ImGui::Spacing();

    // Header stays pinned; the lifecycle board scrolls below.
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(0, 0));
    ImGui::BeginChild("##rel_scroll", ImVec2(0, 0), ImGuiChildFlags_None);
    ImGui::PopStyleVar();
    if (!H.itemsErr.empty()) { plmError(H.itemsErr); ImGui::EndChild(); return; }
    if (H.itemsLoading && H.items.empty()) {
        plmSpinner("Loading");
        ImGui::EndChild();
        return;
    }
    if (H.items.empty() && H.gitLog.empty()) {
        plmEmptyState("(#)", "Nothing to release yet",
                      "Add a design or document, then release it here.");
        ImGui::EndChild();
        return;
    }

    // The lifecycle board: In work / Released / Superseded. Buckets come straight
    // from each item's lifecycle; anything not yet released is in work.
    struct Lane {
        const char* title; const char* hint; const float* tint;
        std::vector<const PlmItemRow*> rows;
    };
    Lane lanes[3] = {
        {"In work", "Nothing waiting — everything here is released.",
         palette.accent, {}},
        {"Released", "No released revisions yet.", palette.statusOk, {}},
        {"Superseded", "No revision has been replaced yet.", palette.textDim, {}},
    };
    for (const auto& it : H.items) {
        if (it.lifecycle == "Released")        lanes[1].rows.push_back(&it);
        else if (it.lifecycle == "Superseded") lanes[2].rows.push_back(&it);
        else                                   lanes[0].rows.push_back(&it);
    }

    // One card per item; the in-work lane carries the inline release form (note +
    // plain-language failure + confirm override) — the SAME doRelease path as
    // before, never a second implementation of release logic.
    auto drawCard = [&](const PlmItemRow& it, int lane) {
        ImGui::PushID(it.itemId.c_str());
        const bool open = (s_releaseItem == it.itemId);
        plmCardBegin("##rel", open, 8.0f, 6.0f, lanes[lane].tint, 0.13f);
        plmDocTypeTag(it.docType, it.kind);
        ImGui::SameLine();
        ImGui::AlignTextToFramePadding();
        plmItemTitle(it.path, nullptr, /*alignFrame=*/true);
        plmLifecycleBadge(it.lifecycle, !it.rev.empty() ? it.rev : it.workingRev);
        if (lane == 0) {
            ImGui::SameLine(0, 12 * S);
            if (ui::kit::secondaryButton(open ? "Cancel" : "Release…")) {
                s_releaseItem = open ? "" : it.itemId;
                s_releaseNote[0] = '\0';
                H.actionErr.clear();
            }
            if (open) {
                ImGui::PushStyleVar(ImGuiStyleVar_FramePadding, ImVec2(6 * S, 4 * S));
                ImGui::SetNextItemWidth(-1);
                ImGui::InputTextWithHint("##relnote",
                                         "release note (what this revision is)",
                                         s_releaseNote, sizeof(s_releaseNote));
                ImGui::PopStyleVar();
                accentButtonColors();
                if (!H.busy && ImGui::Button("Release", ImVec2(120 * S, 0)))
                    doRelease(st, it.itemId, false);
                ImGui::PopStyleColor(2);
                if (!H.actionErr.empty()) {
                    plmError(H.actionErr);
                    ImGui::PushStyleColor(ImGuiCol_Button,
                        ImVec4(palette.statusWarn[0], palette.statusWarn[1],
                               palette.statusWarn[2], 0.6f));
                    if (!H.busy &&
                        ImGui::Button("Release anyway", ImVec2(140 * S, 0)))
                        doRelease(st, it.itemId, true);
                    ImGui::PopStyleColor();
                }
                if (H.busy) plmSpinner("Releasing");
            }
        }
        plmCardEnd();
        ImGui::PopID();
    };
    auto drawLane = [&](int lane, bool first) {
        ui::kit::sectionHeader(lanes[lane].title, nullptr, nullptr,
                               (int)lanes[lane].rows.size(), first);
        if (lanes[lane].rows.empty()) dimText("%s", lanes[lane].hint);
        for (const PlmItemRow* it : lanes[lane].rows) drawCard(*it, lane);
    };

    // Three columns when the pane is wide enough; stacked lanes when it is not
    // (the Files page embeds this board in a half-width panel).
    const bool wide = ImGui::GetContentRegionAvail().x >= ui::metrics::px(620.0f);
    if (wide && ImGui::BeginTable("##rel_board", 3,
                                  ImGuiTableFlags_SizingStretchSame |
                                  ImGuiTableFlags_NoPadOuterX)) {
        ImGui::TableNextRow();
        for (int lane = 0; lane < 3; ++lane) {
            ImGui::TableSetColumnIndex(lane);
            drawLane(lane, true);
        }
        ImGui::EndTable();
    } else {
        for (int lane = 0; lane < 3; ++lane) drawLane(lane, lane == 0);
    }

    if (!H.gitLog.empty()) {
        ImGui::Dummy(ImVec2(0, 8 * S));
        plmSectionHeader("Git history", "git");
        ImGui::Dummy(ImVec2(0, 2 * S));
        drawGitActivity(10);
    }
    ImGui::EndChild();
}

// ---- Team & Sync --------------------------------------------------------------

namespace {

// Kind → user-facing label. SSH/HTTPS name the protocol the user picked in the
// share dialog — that is a deliberate choice, not leaked tool vocabulary.
const char* teamKindLabel(const std::string& kind) {
    if (kind == "folder")         return "Shared folder";
    if (kind == "ssh")            return "Server (SSH)";
    if (kind == "https")          return "Server (HTTPS)";
    if (kind == "plm-server")     return "Live server";
    if (kind == "chudware-cloud") return "Chudware Cloud";
    return "Shared location";
}

const char* teamKindIcon(const std::string& kind) {
    if (kind == "folder")         return "body_folder";
    if (kind == "plm-server")     return "team";
    if (kind == "chudware-cloud") return "app";
    return "vendor_globe";
}

// Share-this-project dialog lifetime. Opening prefills the display name from the
// OS user; closing from the async add-remote callback is deferred one frame
// because CloseCurrentPopup only works inside the popup's own scope.
bool s_shareOpen  = false;
bool s_shareClose = false;

void openShareDialog() {
    auto& H = g_plmHub;
    H.actionErr.clear();
    if (!H.shareDisplay[0]) {
        const char* u = std::getenv("USER");
        if (!u || !u[0]) u = std::getenv("USERNAME");
        if (u && u[0]) std::snprintf(H.shareDisplay, sizeof(H.shareDisplay), "%s", u);
    }
    s_shareOpen = true;
}

// One-button sync against one shared location (contract §6.3–§6.4). The outcome
// is stored per remote and reported in plain language. A same-design collision
// never raises a prompt: the service already preserved BOTH versions as a copy
// (§6.4) and the UI only reports that it happened.
void doTeamSync(AppState& st, const std::string& remoteId) {
    auto& H = g_plmHub;
    if (!H.syncingRemote.empty()) return;
    H.syncingRemote = remoteId;
    if (H.lastSyncRemote == remoteId) { H.lastSyncMsg.clear(); H.lastSyncCopies.clear(); }
    nlohmann::json p = pathParam(st);
    p["remote_id"] = remoteId;
    st.rpc.call("plm_sync", p, [&st, remoteId](const core::RpcResult& r) {
        auto& H = g_plmHub;
        H.syncingRemote.clear();
        H.lastSyncRemote = remoteId;
        H.lastSyncCopies.clear();
        std::string hint;
        if (!plmOk(r, hint)) {
            H.lastSyncOk = false;
            H.lastSyncMsg = plmTeamFriendlyError(plmReason(r), hint);
            st.toast(ui::ToastLevel::Error, H.lastSyncMsg);
            return;
        }
        const bool pulled = r.result.value("pulled", false);
        const bool pushed = r.result.value("pushed", false);
        if (auto c = r.result.find("conflicts_copied"); c != r.result.end() && c->is_array())
            for (const auto& e : *c) {
                if (!e.is_object()) continue;
                std::string n = e.value("name", std::string());
                if (n.empty()) n = e.value("path", std::string());
                if (n.empty()) n = e.value("new_item_id", std::string());
                if (!n.empty()) H.lastSyncCopies.push_back(std::move(n));
            }
        H.lastSyncOk = true;
        if (pulled && pushed)
            H.lastSyncMsg = "Brought in the team's changes and published yours.";
        else if (pulled) H.lastSyncMsg = "Brought in the team's changes.";
        else if (pushed) H.lastSyncMsg = "Published your changes.";
        else             H.lastSyncMsg = "Already up to date.";
        if (!H.lastSyncCopies.empty()) {
            char extra[96];
            std::snprintf(extra, sizeof(extra),
                          " %d design%s edited on both sides — both versions were kept.",
                          (int)H.lastSyncCopies.size(),
                          H.lastSyncCopies.size() == 1 ? " was" : "s were");
            H.lastSyncMsg += extra;
        }
        st.toast(ui::ToastLevel::Success, H.lastSyncMsg);
        plmInvalidate();   // a pull may have rewritten items/history — refetch all
    });
}

void doAddRemote(AppState& st) {
    auto& H = g_plmHub;
    if (H.busy || !H.shareLocation[0]) return;
    H.busy = true; H.actionErr.clear();
    static const char* kKinds[] = {"folder", "ssh", "https"};
    const int k = std::clamp(H.shareKind, 0, 2);
    nlohmann::json p = pathParam(st);
    p["kind"] = kKinds[k];
    p["url"]  = H.shareLocation;
    p["name"] = H.shareName[0] ? H.shareName
                               : (k == 0 ? "Team folder" : "Team server");
    if (H.shareDisplay[0]) p["display_name"] = H.shareDisplay;
    st.rpc.call("plm_add_remote", p, [&st](const core::RpcResult& r) {
        auto& H = g_plmHub;
        H.busy = false;
        std::string hint;
        if (!plmOk(r, hint)) {
            H.actionErr = plmTeamFriendlyError(plmReason(r), hint);
            return;
        }
        H.actionErr.clear();
        H.shareLocation[0] = '\0';
        H.shareName[0] = '\0';
        s_shareClose = true;
        st.toast(ui::ToastLevel::Success,
                 "Connected — press Sync now to share and receive changes");
        plmInvalidate();
    });
}

// Release one of MY advisory locks from the Team page (the quickbar owns
// acquire/override for the active document).
void doTeamUnlock(AppState& st, const std::string& itemId) {
    auto& H = g_plmHub;
    const PlmRemoteRow* rm = plmTeamLockRemote();
    if (H.busy || !rm) return;
    H.busy = true;
    nlohmann::json p = pathParam(st);
    p["item_id"] = itemId;
    p["remote_id"] = rm->id;
    st.rpc.call("plm_unlock", p, [&st](const core::RpcResult& r) {
        g_plmHub.busy = false;
        std::string hint;
        if (!plmOk(r, hint)) {
            st.toast(ui::ToastLevel::Error, plmTeamFriendlyError(plmReason(r), hint));
            return;
        }
        st.toast(ui::ToastLevel::Info, "Editing lock released");
        plmInvalidate();
    });
}

void drawShareProjectDialog(AppState& st) {
    auto& H = g_plmHub;
    const float S = ui::uiScale;
    if (s_shareOpen) {
        ImGui::OpenPopup("Share this project##plm_share");
        s_shareOpen = false;   // consume: the popup now owns its lifetime
    }
    ImGui::SetNextWindowPos(ImGui::GetMainViewport()->GetCenter(),
                            ImGuiCond_Appearing, ImVec2(0.5f, 0.5f));
    if (!ImGui::BeginPopupModal("Share this project##plm_share", nullptr,
                                ImGuiWindowFlags_AlwaysAutoResize))
        return;
    ImGui::PushTextWrapPos(ImGui::GetCursorPosX() + 420 * S);
    dimText("Pick where the team meets: a folder everyone can reach, or a server "
            "you control. Everyone keeps a full private copy of the project and "
            "Sync brings changes together.");
    ImGui::PopTextWrapPos();
    ImGui::Dummy(ImVec2(0, 4 * S));

    struct KindOpt { const char* label; const char* hint; };
    static const KindOpt kOpts[] = {
        {"Shared folder",  "A folder everyone can reach — a network drive or shared disk."},
        {"Server (SSH)",   "Your own server, reached over SSH."},
        {"Server (HTTPS)", "A server you control, reached over HTTPS."},
    };
    for (int i = 0; i < 3; ++i) {
        if (i) ImGui::SameLine();
        const bool sel = (H.shareKind == i);
        ImGui::PushStyleColor(ImGuiCol_Button, sel
            ? ImVec4(palette.accent[0], palette.accent[1], palette.accent[2], 0.30f)
            : ImVec4(palette.button[0], palette.button[1], palette.button[2], 0.5f));
        if (ImGui::Button(kOpts[i].label)) H.shareKind = i;
        ImGui::PopStyleColor();
        if (ImGui::IsItemHovered()) ImGui::SetTooltip("%s", kOpts[i].hint);
    }
    ImGui::SameLine();
    (void)ui::kit::secondaryButton("Chudware Cloud", false, "Not yet available");

    ImGui::Dummy(ImVec2(0, 4 * S));
    if (H.shareKind == 0) {
        dimText("The folder everyone can reach:");
        ImGui::SetNextItemWidth(-(84 * S));
        ImGui::InputTextWithHint("##share_loc", "e.g. a network drive or shared disk",
                                 H.shareLocation, sizeof(H.shareLocation));
        ImGui::SameLine();
        if (ImGui::Button("Browse\xe2\x80\xa6", ImVec2(76 * S, 0))) {
            std::string abs = ui::openFolderDialog();
            if (!abs.empty())
                std::snprintf(H.shareLocation, sizeof(H.shareLocation), "%s",
                              abs.c_str());
        }
    } else {
        dimText("The server address for this project:");
        ImGui::SetNextItemWidth(-1);
        ImGui::InputTextWithHint("##share_loc",
            H.shareKind == 1 ? "e.g. user@host:projects/gearbox"
                             : "e.g. https://host/projects/gearbox",
            H.shareLocation, sizeof(H.shareLocation));
    }

    ImGui::Dummy(ImVec2(0, 4 * S));
    dimText("You'll appear to the team as:");
    ImGui::SetNextItemWidth(240 * S);
    ImGui::InputTextWithHint("##share_disp", "your name",
                             H.shareDisplay, sizeof(H.shareDisplay));
    dimText("Name this location (optional):");
    ImGui::SetNextItemWidth(240 * S);
    ImGui::InputTextWithHint("##share_name",
                             H.shareKind == 0 ? "Team folder" : "Team server",
                             H.shareName, sizeof(H.shareName));

    ui::kit::inlineStatus(H.actionErr.c_str(), ui::kit::Tone::Err);
    ImGui::Dummy(ImVec2(0, 6 * S));
    if (H.busy) {
        plmSpinner("Connecting");
    } else {
        if (ui::kit::primaryButton("Connect", H.shareLocation[0] != '\0',
                                   "Pick a folder or enter an address first"))
            doAddRemote(st);
        ImGui::SameLine();
        if (ui::kit::secondaryButton("Cancel")) {
            H.actionErr.clear();
            ImGui::CloseCurrentPopup();
        }
    }
    if (s_shareClose) { s_shareClose = false; ImGui::CloseCurrentPopup(); }
    ImGui::EndPopup();
}

// One shared-location card: kind + capabilities, where it lives, how you appear
// to the team there, the Sync button and the last sync's plain-language result.
void drawRemoteCard(AppState& st, const PlmRemoteRow& rm) {
    auto& H = g_plmHub;
    ImGui::PushID(rm.id.c_str());
    (void)ui::kit::cardBegin("##remote", rm.name.c_str(), teamKindIcon(rm.kind));
    ui::kit::chip(teamKindLabel(rm.kind),
                  rm.reserved ? ui::kit::Tone::Warn : ui::kit::Tone::Accent);
    if (rm.reserved) {
        ImGui::SameLine();
        ui::kit::chip("Not yet available", ui::kit::Tone::Warn, /*dot=*/true);
    }
    if (rm.canLive) {
        ImGui::SameLine();
        ui::kit::chip("Live editing", ui::kit::Tone::Ok, /*dot=*/true,
                      "Everyone edits one design together \xe2\x80\x94 the server "
                      "keeps a single ordered history. No Sync, no locks.");
    }
    if (rm.canLock) {
        ImGui::SameLine();
        ui::kit::chip("Editing locks", ui::kit::Tone::Ok, /*dot=*/true,
                      "This location shares who is editing what.");
    }
    if (!rm.url.empty())
        ui::kit::keyValueRow("Location", rm.url.c_str(), ui::kit::Tone::Neutral,
                             /*copyable=*/true);
    if (!rm.identityDisplay.empty())
        ui::kit::keyValueRow("You appear as", rm.identityDisplay.c_str());

    const bool syncing = (H.syncingRemote == rm.id);
    if (rm.canLive) {
        // A live server orders every edit itself — there is nothing to Sync and
        // no advisory lock to take. The card shows the session instead.
        auto& L = H.live;
        const bool here = L.joined && L.remoteId == rm.id;
        if (here) {
            ui::kit::chip(L.connected ? "LIVE" : "LIVE \xc2\xb7 reconnecting",
                          L.connected ? ui::kit::Tone::Ok : ui::kit::Tone::Warn,
                          /*dot=*/true,
                          L.connected
                              ? "This machine is in a live editing session."
                              : "The connection dropped \xe2\x80\x94 reconnecting; "
                                "edits resume once it is back.");
            std::string itemName = L.itemId;
            for (const auto& it : H.items)
                if (it.itemId == L.itemId) {
                    if (!it.path.empty()) itemName = it.path;
                    break;
                }
            ui::kit::keyValueRow("Editing live", itemName.c_str());
            std::string who;
            for (const auto& peer : L.peers) {
                if (!who.empty()) who += ", ";
                who += peer.display;
            }
            ui::kit::keyValueRow("Also here",
                                 who.empty() ? "no one else yet" : who.c_str());
            if (L.leaving)
                ui::kit::spinner("Leaving");
            else if (ui::kit::secondaryButton("Leave live editing"))
                plmLiveLeave(st);
        } else if (L.joining && L.remoteId == rm.id) {
            ui::kit::spinner("Joining live session");
        } else {
            dimText("Open a tracked document and pick \"Join live editing\" from "
                    "its PLM chip \xe2\x80\x94 everyone in the session sees one "
                    "design, ordered by this server.");
        }
        if (!L.err.empty() && L.remoteId == rm.id)
            ui::kit::inlineStatus(L.err.c_str(), ui::kit::Tone::Err);
    } else if (syncing) {
        ui::kit::spinner("Syncing");
    } else if (rm.reserved) {
        (void)ui::kit::primaryButton("Sync now", false,
                                     "Chudware Cloud is not yet available");
    } else {
        const bool idle = H.syncingRemote.empty();
        if (ui::kit::primaryButton("Sync now", rm.canSync && idle,
                                   !rm.canSync ? "This location does not support sync"
                                               : "Another sync is still running"))
            doTeamSync(st, rm.id);
    }
    if (!rm.canLive && !syncing && H.lastSyncRemote == rm.id && !H.lastSyncMsg.empty()) {
        ui::kit::inlineStatus(H.lastSyncMsg.c_str(),
                              H.lastSyncOk ? ui::kit::Tone::Ok : ui::kit::Tone::Err);
        for (const auto& n : H.lastSyncCopies) {
            const std::string line = "Both versions kept: " + n;
            ui::kit::inlineStatus(line.c_str(), ui::kit::Tone::Warn);
        }
    }
    ui::kit::cardEnd();
    ImGui::PopID();
}

// The "who's editing what" list — every advisory lock on the shared location.
// Locks warn, they never block (contract §6.2); expired ones stay visible but
// read as history, not presence.
void drawTeamLocks(AppState& st) {
    auto& H = g_plmHub;
    if (H.remotes.empty()) {
        dimText("Editing locks appear here once the project is shared.");
        return;
    }
    const PlmRemoteRow* me = plmTeamLockRemote();
    if (!me) {
        dimText("This shared location does not support editing locks.");
        return;
    }
    if (H.locks.empty()) {
        dimText("No one is holding an editing lock right now. Take one from the "
                "PLM chip on an open document.");
        return;
    }
    for (const auto& l : H.locks) {
        const bool live = plmLockLive(l);
        const bool mine = !me->identityUserId.empty() &&
                          l.userId == me->identityUserId;
        ImGui::PushID(l.itemId.c_str());
        plmCardBegin("##lock", false, 8.0f, 4.0f,
                     live ? (mine ? palette.accent : palette.statusWarn)
                          : palette.textDim, 0.12f);
        std::string itemName = l.itemId;
        for (const auto& it : H.items)
            if (it.itemId == l.itemId) {
                if (!it.path.empty()) itemName = it.path;
                break;
            }
        plmItemTitle(itemName);
        ui::kit::chip(live ? (mine ? "You are editing" : "Editing") : "Expired",
                      live ? (mine ? ui::kit::Tone::Ok : ui::kit::Tone::Warn)
                           : ui::kit::Tone::Neutral, /*dot=*/true);
        const std::string who = mine ? std::string("You")
                                     : (l.display.empty() ? l.userId : l.display);
        std::string sub = who + "  \xc2\xb7  since " + plmNiceTime(l.acquiredAt);
        if (!l.expiresAt.empty()) sub += "  \xc2\xb7  until " + plmNiceTime(l.expiresAt);
        dimText("%s", sub.c_str());
        if (mine && live) {
            plmActionRowBegin();
            if (ImGui::SmallButton("Release") && !H.busy) doTeamUnlock(st, l.itemId);
            plmActionRowEnd();
        }
        plmCardEnd();
        ImGui::PopID();
    }
}

}  // namespace

void plmDrawTeamPage(AppState& st) {
    auto& H = g_plmHub;
    const float S = ui::uiScale;
    plmEnsureTeam(st);
    plmEnsureItems(st);   // lock rows name items by path, not raw ids

    plmSectionHeader("Team & Sync", "team");
    dimText("Share a project through a shared folder or your own server. Everyone "
            "keeps working; Sync brings changes together without ever merging or "
            "losing work — if two people change the same design, both versions "
            "are kept.");
    ImGui::Dummy(ImVec2(0, 6 * S));

    if (H.teamLoading && H.remotes.empty() && H.teamErr.empty()) {
        plmSpinner("Checking shared locations");
        return;
    }
    if (!H.teamErr.empty()) {
        plmError(H.teamErr);
        if (ui::kit::secondaryButton("Try again")) {
            H.teamErr.clear();
            H.teamFetchedAt = -1.0e9;   // force the next plmEnsureTeam to refetch
        }
        return;
    }

    const float pgGap = 12 * S;
    const float colW  = std::floor((ImGui::GetContentRegionAvail().x - pgGap) / 2.0f);

    plmPanelBegin("##team_remotes", "Shared locations", "vendor_globe", colW);
    if (H.remotes.empty()) {
        if (ui::kit::emptyState("vendor_globe", "Not shared yet",
                "Connect a shared folder or a server you control, and everyone "
                "on the team can open, edit and sync this project.",
                "Share this project\xe2\x80\xa6"))
            openShareDialog();
    } else {
        for (const auto& rm : H.remotes) drawRemoteCard(st, rm);
        ImGui::Dummy(ImVec2(0, 2 * S));
        if (ui::kit::secondaryButton("Share another location\xe2\x80\xa6"))
            openShareDialog();
    }
    plmPanelEnd();

    ImGui::SameLine(0, pgGap);
    plmPanelBegin("##team_locks", "Who's editing what", "team", colW);
    dimText("Editing locks are a courtesy signal — they warn, they never block. "
            "Take or release one from the PLM chip on an open document.");
    ImGui::Dummy(ImVec2(0, 2 * S));
    drawTeamLocks(st);
    if (H.teamLoading && !H.remotes.empty()) plmSpinner("Refreshing");
    plmPanelEnd();

    drawShareProjectDialog(st);
}

// ---- Settings -----------------------------------------------------------------

void plmDrawSettingsPage(AppState& st) {
    auto& H = g_plmHub;
    const float S = ui::uiScale;
    plmEnsureSettings(st);

    plmSectionHeader("Settings", "settings");
    if (!H.setErr.empty()) { plmError(H.setErr); return; }

    // ---- Two-column panel grid — the setting groups are horizontally compressed
    //      to a half-width column and tiled side by side so the page fills the
    //      screen with minimal deadspace instead of one narrow full-width stack. --
    const float pgGap = 12 * S;
    const float colW  = std::floor((ImGui::GetContentRegionAvail().x - pgGap) / 2.0f);

    // Row 1 · left: part-number scheme
    plmPanelBegin("##set_pn", "Part-number scheme", "new_doc", colW);
    dimText("New items are numbered <prefix>-0001, <prefix>-0002, … You can always "
            "rename a number later.");
    if (H.prefixBuf[0] == '\0' && !H.partPrefix.empty())
        std::snprintf(H.prefixBuf, sizeof(H.prefixBuf), "%s", H.partPrefix.c_str());
    ImGui::PushStyleVar(ImGuiStyleVar_FramePadding, ImVec2(6 * S, 4 * S));
    ImGui::SetNextItemWidth(160 * S);
    ImGui::InputTextWithHint("##prefix", "PRJ", H.prefixBuf, sizeof(H.prefixBuf));
    ImGui::PopStyleVar();
    ImGui::SameLine();
    accentButtonColors();
    if (!H.busy && ImGui::Button("Save", ImVec2(80 * S, 0)) && H.prefixBuf[0]) {
        H.busy = true;
        nlohmann::json p = pathParam(st);
        p["prefix"] = H.prefixBuf;
        st.rpc.call("plm_scheme_set", p, [&st](const core::RpcResult& r) {
            g_plmHub.busy = false;
            std::string h; if (!plmOk(r, h)) { st.toast(ui::ToastLevel::Error, h); return; }
            st.toast(ui::ToastLevel::Success, "Part-number scheme updated");
            plmInvalidate();
        });
    }
    ImGui::PopStyleColor(2);
    plmPanelEnd();

    // Row 1 · right: AI assistance
    ImGui::SameLine(0, pgGap);
    plmPanelBegin("##set_ai", "AI assistance", "health_check", colW);
    dimText("How much the assistant helps with history. Manual controls are always "
            "available. Managed lets the AI record checkpoints itself at task "
            "boundaries (no env flag needed; overrides CHUDWARE_PLM_BRIDGE).");
    struct Mode { const char* id; const char* label; };
    static const Mode kModes[] = {
        {"manual", "Manual"}, {"assisted", "Assisted"}, {"managed", "Managed"},
    };
    for (int i = 0; i < 3; ++i) {
        if (i) ImGui::SameLine();
        bool sel = (H.aiMode == kModes[i].id);
        ImGui::PushStyleColor(ImGuiCol_Button, sel
            ? ImVec4(palette.accent[0], palette.accent[1], palette.accent[2], 0.30f)
            : ImVec4(palette.button[0], palette.button[1], palette.button[2], 0.5f));
        if (ImGui::Button(kModes[i].label) && !H.busy && !sel) {
            H.busy = true;
            nlohmann::json p = pathParam(st);
            p["mode"] = kModes[i].id;
            st.rpc.call("plm_set_ai_mode", p, [&st](const core::RpcResult& r) {
                g_plmHub.busy = false;
                std::string h; if (!plmOk(r, h)) { st.toast(ui::ToastLevel::Error, h); return; }
                st.toast(ui::ToastLevel::Info, "Assistance level updated");
                plmInvalidate();
            });
        }
        ImGui::PopStyleColor();
    }
    ImGui::Dummy(ImVec2(0, 8 * S));
    dimText("Automatically record every save into project history.");
    bool ac = H.autoCommit;
    if (ImGui::Checkbox("Automatically record saves to history", &ac) && !H.busy) {
        H.busy = true;
        H.autoCommit = ac;  // optimistic — plmAutoRecordSave honors it at once
        nlohmann::json p = pathParam(st);
        p["auto_commit"] = ac;
        st.rpc.call("plm_settings_set", p, [&st, ac](const core::RpcResult& r) {
            g_plmHub.busy = false;
            std::string h;
            if (!plmOk(r, h)) {
                g_plmHub.autoCommit = !ac;
                st.toast(ui::ToastLevel::Error, h);
                return;
            }
            st.toast(ui::ToastLevel::Info,
                     ac ? "Saves will be recorded to history"
                        : "Auto-record turned off");
            plmInvalidate();
        });
    }
    plmPanelEnd();

    ImGui::Dummy(ImVec2(0, pgGap));

    // Row 2 · left: Organization & access (preview — applies once a team is connected)
    plmPanelBegin("##set_org", "Organization & access", "settings", colW);
    dimText("Who can see and change this project. These apply once you connect a "
            "team (see Team & Sync).");

    // project visibility
    ImGui::Dummy(ImVec2(0, 2 * S));
    ImGui::TextUnformatted("Visibility");
    struct Opt { const char* id; const char* label; };
    static const Opt kVis[] = {
        {"private", "Private"}, {"team", "Team"},
        {"org", "Organization"}, {"public", "Public"},
    };
    static int s_vis = 1;  // Team
    for (int i = 0; i < 4; ++i) {
        if (i) ImGui::SameLine();
        bool sel = (s_vis == i);
        ImGui::PushStyleColor(ImGuiCol_Button, sel
            ? ImVec4(palette.accent[0], palette.accent[1], palette.accent[2], 0.30f)
            : ImVec4(palette.button[0], palette.button[1], palette.button[2], 0.5f));
        if (ImGui::Button(kVis[i].label)) s_vis = i;
        ImGui::PopStyleColor();
    }

    // default role for new members
    ImGui::Dummy(ImVec2(0, 4 * S));
    ImGui::TextUnformatted("Default role for new members");
    static const char* kRoles[] = {"Viewer", "Commenter", "Editor", "Manager"};
    static int s_role = 0;
    ImGui::SetNextItemWidth(180 * S);
    ImGui::Combo("##defrole", &s_role, kRoles, IM_ARRAYSIZE(kRoles));

    // release / edit governance
    ImGui::Dummy(ImVec2(0, 4 * S));
    ImGui::TextUnformatted("Governance");
    static bool s_reqApproval = false, s_lockOnEdit = true, s_restrictRelease = true;
    ImGui::Checkbox("Require approval before a release", &s_reqApproval);
    ImGui::Checkbox("Take an advisory lock while a document is open", &s_lockOnEdit);
    ImGui::Checkbox("Only Managers can release", &s_restrictRelease);

    ImGui::Dummy(ImVec2(0, 2 * S));
    dimText("Preview — permissions are enforced server-side once team sharing is "
            "enabled; nothing here changes single-machine use.");
    plmPanelEnd();

    // Row 2 · right: Team & Sync summary — the real controls live on the Team tab.
    ImGui::SameLine(0, pgGap);
    plmPanelBegin("##set_team", "Team & Sync", "team", colW);
    plmEnsureTeam(st);
    if (H.remotes.empty()) {
        dimText("Not shared yet. Connect a shared folder or your own server from "
                "the Team & Sync page — everyone keeps working and Sync brings "
                "changes together without ever merging or losing work.");
    } else {
        dimText("%d shared location%s connected.", (int)H.remotes.size(),
                H.remotes.size() == 1 ? "" : "s");
        for (const auto& rm : H.remotes)
            ui::kit::keyValueRow(rm.name.c_str(), teamKindLabel(rm.kind));
    }
    ImGui::Dummy(ImVec2(0, 4 * S));
    if (ui::kit::secondaryButton("Open Team & Sync")) H.page = PlmPage::Team;
    plmPanelEnd();
}

// ---- Planning (kanban + milestones) -------------------------------------------

namespace {

void planCall(AppState& st, const char* op, const char* entity,
              const nlohmann::json& data, const std::string& id = "") {
    auto& H = g_plmHub;
    if (H.busy) return;
    H.busy = true;
    nlohmann::json p = pathParam(st);
    p["op"] = op;
    p["entity"] = entity;
    if (!data.is_null()) p["data"] = data;
    if (!id.empty()) p["id"] = id;
    st.rpc.call("plm_plan", p, [&st](const core::RpcResult& r) {
        g_plmHub.busy = false;
        std::string h; if (!plmOk(r, h)) { st.toast(ui::ToastLevel::Error, h); return; }
        plmInvalidate();
    });
}

// A compact, interactive calendar popup bound to a "YYYY-MM-DD" text buffer.
// The trigger button shows the current value (or a hint); the popup lets the
// user page months and click a day. Returns true when the value changed.
bool plmDatePicker(const char* id, char* buf, size_t n) {
    const float S = ui::uiScale;
    bool changed = false;

    // parse the buffer (fall back to today for the initial view)
    int y = 0, mo = 0, d = 0;
    bool haveVal = (std::sscanf(buf, "%d-%d-%d", &y, &mo, &d) == 3 &&
                    mo >= 1 && mo <= 12 && d >= 1 && d <= 31);
    const std::time_t tt = std::time(nullptr);
    std::tm now{};
    const bool haveNow = chudware::util::localTime(tt, now);
    if (!haveVal) {
        if (!haveNow) return false;
        y = now.tm_year + 1900;
        mo = now.tm_mon + 1;
        d = 0;
    }

    ImGui::PushID(id);
    // view state (month being browsed) lives in ImGui storage, keyed per widget
    ImGuiStorage* store = ImGui::GetStateStorage();
    ImGuiID kY = ImGui::GetID("vy"), kM = ImGui::GetID("vm");
    char label[64];
    if (haveVal) std::snprintf(label, sizeof(label), "%04d-%02d-%02d", y, mo, d);
    else         std::snprintf(label, sizeof(label), "Pick a date\xe2\x80\xa6");
    if (ImGui::Button(label, ImVec2(150 * S, 0))) {
        store->SetInt(kY, y);
        store->SetInt(kM, mo);
        ImGui::OpenPopup("##cal");
    }
    ImGui::SameLine();
    if (haveVal && ImGui::SmallButton("Clear")) { buf[0] = '\0'; changed = true; }

    if (ImGui::BeginPopup("##cal")) {
        // Fixed cell geometry + a tight, uniform column pitch so the weekday header,
        // the leading blanks and the day buttons all sit on the SAME grid. (The default
        // 8px ItemSpacing plus variable-width header text made the columns drift, which
        // read as the day numbers being off-centre under their headers.)
        const float kCell = 26 * S, kCellH = 22 * S, kGap = 2 * S;
        const float kGridW = kCell * 7 + kGap * 6;  // full week row width
        ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing, ImVec2(kGap, kGap));
        ImGui::PushStyleVar(ImGuiStyleVar_ButtonTextAlign, ImVec2(0.5f, 0.5f));

        int vy = store->GetInt(kY, y), vm = store->GetInt(kM, mo);
        // header: < Month Year >  (arrows pinned to the grid edges, title centred between)
        if (ImGui::ArrowButton("##pm", ImGuiDir_Left)) {
            if (--vm < 1) { vm = 12; --vy; }
            store->SetInt(kY, vy); store->SetInt(kM, vm);
        }
        static const char* kMon[] = {"Jan","Feb","Mar","Apr","May","Jun",
                                     "Jul","Aug","Sep","Oct","Nov","Dec"};
        char hdr[32]; std::snprintf(hdr, sizeof(hdr), "%s %d", kMon[vm - 1], vy);
        float hw = ImGui::CalcTextSize(hdr).x;
        float rowX0 = ImGui::GetCursorPosX();  // left edge of the arrow just drawn's row
        ImGui::SameLine();
        ImGui::SetCursorPosX(rowX0 + (kGridW - hw) * 0.5f);
        ImGui::AlignTextToFramePadding();
        ImGui::TextUnformatted(hdr);
        ImGui::SameLine(rowX0 + kGridW - ImGui::GetFrameHeight());
        if (ImGui::ArrowButton("##nm", ImGuiDir_Right)) {
            if (++vm > 12) { vm = 1; ++vy; }
            store->SetInt(kY, vy); store->SetInt(kM, vm);
        }

        ImDrawList* dl = ImGui::GetWindowDrawList();
        // Every cell (weekday label and day number) is drawn the same way: reserve a
        // fixed square with a Dummy/InvisibleButton, then draw the glyph centred both
        // horizontally AND vertically inside that square via the draw list. This is
        // immune to font metrics / ButtonTextAlign quirks — the number always sits
        // dead-centre in its box.
        auto centreText = [&](ImVec2 rmin, ImVec2 rmax, const char* txt, ImU32 col) {
            ImVec2 ts = ImGui::CalcTextSize(txt);
            dl->AddText(ImVec2((rmin.x + rmax.x - ts.x) * 0.5f,
                               (rmin.y + rmax.y - ts.y) * 0.5f), col, txt);
        };

        // weekday header
        static const char* kDow[] = {"Su","Mo","Tu","We","Th","Fr","Sa"};
        ImU32 dimCol = ImGui::ColorConvertFloat4ToU32(
            ImVec4(palette.textDim[0], palette.textDim[1], palette.textDim[2], 1.0f));
        for (int i = 0; i < 7; ++i) {
            if (i) ImGui::SameLine();
            ImVec2 p = ImGui::GetCursorScreenPos();
            ImGui::Dummy(ImVec2(kCell, kCellH));
            centreText(p, ImVec2(p.x + kCell, p.y + kCellH), kDow[i], dimCol);
        }

        // day grid — first weekday + day count via std::tm normalisation
        std::tm first{}; first.tm_year = vy - 1900; first.tm_mon = vm - 1;
        first.tm_mday = 1; std::mktime(&first);
        int lead = first.tm_wday;  // 0=Sun
        static const int mdays[] = {31,28,31,30,31,30,31,31,30,31,30,31};
        int dim = mdays[vm - 1];
        if (vm == 2 && ((vy % 4 == 0 && vy % 100 != 0) || vy % 400 == 0)) dim = 29;

        ImU32 txtCol = ImGui::ColorConvertFloat4ToU32(
            ImVec4(palette.text[0], palette.text[1], palette.text[2], 1.0f));
        int cell = 0;
        for (int i = 0; i < lead; ++i, ++cell) {
            if (cell % 7) ImGui::SameLine();
            ImGui::Dummy(ImVec2(kCell, kCellH));
        }
        for (int day = 1; day <= dim; ++day, ++cell) {
            if (cell % 7) ImGui::SameLine();
            ImGui::PushID(day);
            bool isSel = (haveVal && vy == y && vm == mo && day == d);
            bool isToday = haveNow && vy == now.tm_year + 1900 && vm == now.tm_mon + 1 &&
                           day == now.tm_mday;
            ImVec2 rmin = ImGui::GetCursorScreenPos();
            bool click = ImGui::InvisibleButton("##d", ImVec2(kCell, kCellH));
            ImVec2 rmax(rmin.x + kCell, rmin.y + kCellH);
            bool hov = ImGui::IsItemHovered();
            if (isSel)
                dl->AddRectFilled(rmin, rmax, ImGui::ColorConvertFloat4ToU32(
                    ImVec4(palette.accent[0], palette.accent[1], palette.accent[2], 0.85f)), 4 * S);
            else if (isToday)
                dl->AddRectFilled(rmin, rmax, ImGui::ColorConvertFloat4ToU32(
                    ImVec4(palette.accent[0], palette.accent[1], palette.accent[2], 0.28f)), 4 * S);
            else if (hov)
                dl->AddRectFilled(rmin, rmax, ImGui::ColorConvertFloat4ToU32(
                    ImVec4(palette.text[0], palette.text[1], palette.text[2], 0.10f)), 4 * S);
            char db[4]; std::snprintf(db, sizeof(db), "%d", day);
            centreText(rmin, rmax, db, txtCol);
            if (click) {
                std::snprintf(buf, n, "%04d-%02d-%02d", vy, vm, day);
                changed = true;
                ImGui::CloseCurrentPopup();
            }
            ImGui::PopID();
        }
        ImGui::PopStyleVar(2);
        ImGui::EndPopup();
    }
    ImGui::PopID();
    return changed;
}

// Priority is conveyed by the card's own background tint (blue/yellow/red — see
// plmTintPriority), not a dot drawn over the title — the dot used to sit on top of
// wrapped title text. This just supplies the hover tooltip naming the priority;
// call it while the card's child window is still current (before plmCardEnd) so
// IsWindowHovered() covers the whole card, not a corner pixel.
void priorityDot(const std::string& pr) {
    if (!ImGui::IsWindowHovered()) return;
    std::string p = pr.empty() ? "p2" : pr;
    const char* word = p == "p0" ? "critical" : p == "p1" ? "high"
                     : p == "p3" ? "low" : "med";
    ImGui::SetTooltip("priority: %s", word);
}

static std::string s_selTask;     // selected kanban task (reveals its controls)
static std::string s_selMs;       // selected milestone row
static std::string s_selIssue2;   // selected planning-page issue row

}  // namespace

// A milestone's inline read / edit row (condensed for the right-hand column).
static void drawMilestoneRow(AppState& st, const PlmMilestoneRow& m) {
    auto& H = g_plmHub;
    const float S = ui::uiScale;
    bool done = (m.status == "done");
    bool editing = (H.msEditId == m.id);
    ImGui::PushID(m.id.c_str());
    plmCardBegin("##ms", editing, 8.0f, 3.0f,
                 done ? palette.statusOk : palette.accent, 0.15f);
    if (editing) {
        ImGui::PushStyleVar(ImGuiStyleVar_FramePadding, ImVec2(5 * S, 3 * S));
        ImGui::SetNextItemWidth(-1);
        ImGui::InputTextWithHint("##met", "title", H.msEditTitle, sizeof(H.msEditTitle));
        ImGui::PopStyleVar();
        plmDatePicker("##med", H.msEditDue, sizeof(H.msEditDue));
        ImGui::SameLine();
        accentButtonColors();
        if (ImGui::SmallButton("Save") && H.msEditTitle[0]) {
            nlohmann::json d = {{"title", H.msEditTitle}};
            d["due"] = H.msEditDue[0] ? H.msEditDue : "";
            planCall(st, "update", "milestone", d, m.id);
            H.msEditId.clear();
        }
        ImGui::PopStyleColor(2);
        ImGui::SameLine();
        if (ImGui::SmallButton("Cancel")) H.msEditId.clear();
    } else {
        bool sel = (s_selMs == m.id);
        // status badge + wrapping title, always visible
        ImGui::PushStyleColor(ImGuiCol_Text, done
            ? ImVec4(palette.statusOk[0], palette.statusOk[1], palette.statusOk[2], 1.0f)
            : ImVec4(palette.accent[0], palette.accent[1], palette.accent[2], 1.0f));
        ImGui::AlignTextToFramePadding();
        ImGui::TextUnformatted(done ? "Done" : "Open");
        ImGui::PopStyleColor();
        ImGui::SameLine();
        plmItemTitle(m.title, nullptr, /*alignFrame=*/true);
        // due date — dimmed, pinned to the right of the title row (info, always on)
        if (!m.due.empty()) {
            char due[48]; std::snprintf(due, sizeof(due), "due %s", m.due.c_str());
            float dw = ImGui::CalcTextSize(due).x;
            float rx = ImGui::GetContentRegionMax().x - dw;
            if (rx > ImGui::GetCursorPosX()) {
                ImGui::SameLine(rx);
                ImGui::AlignTextToFramePadding();
                ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(
                    palette.textDim[0], palette.textDim[1], palette.textDim[2], 1.0f));
                ImGui::TextUnformatted(due);
                ImGui::PopStyleColor();
            } else {
                dimText("due %s", m.due.c_str());  // narrow card: wrap to next line
            }
        }
        // admin controls — hidden until the row is selected
        if (sel) {
            plmActionRowBegin();
            if (ImGui::SmallButton("Edit")) {
                H.msEditId = m.id;
                std::snprintf(H.msEditTitle, sizeof(H.msEditTitle), "%s", m.title.c_str());
                std::snprintf(H.msEditDue, sizeof(H.msEditDue), "%s", m.due.c_str());
            }
            ImGui::SameLine();
            if (ImGui::SmallButton(done ? "Reopen" : "Complete"))
                planCall(st, "update", "milestone", {{"status", done ? "open" : "done"}}, m.id);
            ImGui::SameLine();
            ImGui::PushStyleColor(ImGuiCol_Button,
                ImVec4(palette.statusErr[0], palette.statusErr[1], palette.statusErr[2], 0.5f));
            if (ImGui::SmallButton("Del")) planCall(st, "delete", "milestone", nullptr, m.id);
            ImGui::PopStyleColor();
            plmActionRowEnd();
        }
    }
    bool msClicked = plmCardEnd();
    if (!editing && msClicked) s_selMs = (s_selMs == m.id) ? "" : m.id;
    ImGui::PopID();
}

// One issue row on the Planning page: severity + title, with visible actions —
// including "Make plan", which spins the issue into a linked task so you can
// organise fixes right where you see the problems.
static void drawIssueRow(AppState& st, const PlmIssueRow& is) {
    const float S = ui::uiScale;
    (void)S;
    bool closed = (is.state == "closed");
    bool sel = (s_selIssue2 == is.id);
    ImGui::PushID(is.id.c_str());
    plmCardBegin("##is", sel, 8.0f, 3.0f, plmTintSeverity(is.severity), 0.16f);
    const float* sc = palette.textDim;
    if (is.severity == "critical" || is.severity == "high") sc = palette.statusErr;
    else if (is.severity == "med") sc = palette.statusWarn;
    // severity badge + wrapping title (plain text, so a click selects the row)
    ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(sc[0], sc[1], sc[2], 1.0f));
    ImGui::AlignTextToFramePadding();
    ImGui::Text("[%s]", is.severity.empty() ? "med" : is.severity.c_str());
    ImGui::PopStyleColor();
    ImGui::SameLine();
    plmItemTitle(closed ? "(closed) " + is.title : is.title,
                 closed ? palette.textDisabled : nullptr, /*alignFrame=*/true);
    // admin controls — hidden until the row is selected
    if (sel) {
        plmActionRowBegin();
        accentButtonColors();
        if (ImGui::SmallButton("Make plan")) {
            nlohmann::json d = {{"title", is.title},
                                {"links", nlohmann::json::array({
                                    {{"kind", "issue"}, {"id", is.id}}})}};
            planCall(st, "create", "task", d);
            st.toast(ui::ToastLevel::Success, "Added a task from this issue");
        }
        ImGui::PopStyleColor(2);
        ImGui::SameLine();
        // cycle the severity/priority in place: low → med → high → critical → low
        static const char* kSevCycle[] = {"low", "med", "high", "critical"};
        if (ImGui::SmallButton("Priority")) {
            int cur = 1;  // default med
            for (int k = 0; k < 4; ++k) if (is.severity == kSevCycle[k]) cur = k;
            planCall(st, "update", "issue", {{"severity", kSevCycle[(cur + 1) % 4]}}, is.id);
        }
        ImGui::SameLine();
        if (ImGui::SmallButton(closed ? "Reopen" : "Close"))
            planCall(st, "update", "issue", {{"state", closed ? "open" : "closed"}}, is.id);
        ImGui::SameLine();
        ImGui::PushStyleColor(ImGuiCol_Button,
            ImVec4(palette.statusErr[0], palette.statusErr[1], palette.statusErr[2], 0.5f));
        if (ImGui::SmallButton("Del")) planCall(st, "delete", "issue", nullptr, is.id);
        ImGui::PopStyleColor();
        plmActionRowEnd();
    }
    if (plmCardEnd()) s_selIssue2 = sel ? "" : is.id;
    ImGui::PopID();
}

void plmDrawPlanningPage(AppState& st) {
    auto& H = g_plmHub;
    const float S = ui::uiScale;
    plmEnsurePlanning(st);
    plmEnsureGit(st);        // fold GitHub issues into the Issues panel below

    if (!H.planErr.empty()) plmError(H.planErr);

    static const char* kSev[] = {"low", "med", "high", "critical"};

    // ---- top band: Issues + Milestones as two discrete glass panels ------------
    const float availTop = ImGui::GetContentRegionAvail().x;
    const float halfW = (availTop - 12 * S) * 0.5f;

    // Issues — log a problem with a severity, so you can plan fixes while you see
    // what's wrong. "Make plan" (on each row) turns one into a linked task.
    plmPanelBegin("##pl_issues", "Issues", "issue_bug", halfW);
    ui::kit::fieldEnum("##newissuesev", "Severity", &H.newIssueSev, kSev, IM_ARRAYSIZE(kSev),
                      ui::kit::FieldOpts().withEnumStyle(ui::kit::EnumStyle::Combo));
    ImGui::SetNextItemWidth(-96 * S);
    bool ienter = ImGui::InputTextWithHint("##newissue", "describe an issue\xe2\x80\xa6",
        H.newIssueTitle, sizeof(H.newIssueTitle), ImGuiInputTextFlags_EnterReturnsTrue);
    ImGui::SameLine();
    if ((plmGlassButton("##logbtn", "Log", "new_doc", true, 88 * S) || ienter) &&
        H.newIssueTitle[0]) {
        planCall(st, "create", "issue",
                 {{"title", H.newIssueTitle}, {"severity", kSev[H.newIssueSev]}});
        H.newIssueTitle[0] = '\0';
    }
    ImGui::Dummy(ImVec2(0, 4 * S));
    // Only the item list scrolls — the header + add-row above stay pinned.
    ImGui::PushStyleColor(ImGuiCol_ChildBg, ImVec4(0, 0, 0, 0));
    ImGui::BeginChild("##isslist", ImVec2(0, 168 * S), ImGuiChildFlags_None,
                      ImGuiWindowFlags_NavFlattened);
    if (H.issues.empty()) dimText("No issues logged.");
    // Sort by severity (critical first) without disturbing the shared cache.
    std::vector<const PlmIssueRow*> issueView;
    issueView.reserve(H.issues.size());
    for (const auto& is : H.issues) issueView.push_back(&is);
    std::stable_sort(issueView.begin(), issueView.end(),
        [](const PlmIssueRow* a, const PlmIssueRow* b) {
            return plmSeverityRank(a->severity) < plmSeverityRank(b->severity);
        });
    for (const auto* is : issueView) drawIssueRow(st, *is);
    // GitHub issues (read-only) — surfaced here once the project's git repo is
    // connected. "Make plan" converts one into a native, linkable task.
    if (!H.gitIssues.empty()) {
        ImGui::Dummy(ImVec2(0, 4 * S));
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(palette.textDim[0], palette.textDim[1], palette.textDim[2], 1.0f));
        ImGui::TextUnformatted("From GitHub");
        ImGui::PopStyleColor();
        for (const auto& gi : H.gitIssues) {
            ImGui::PushID(("ghi" + std::to_string(gi.number)).c_str());
            plmCardBegin("##ghis", false, 8.0f, 3.0f, palette.accent, 0.14f);
            ImGui::PushStyleColor(ImGuiCol_Text,
                ImVec4(palette.accent[0], palette.accent[1], palette.accent[2], 1.0f));
            ImGui::AlignTextToFramePadding();
            ImGui::Text("#%d", gi.number);
            ImGui::PopStyleColor();
            ImGui::SameLine();
            plmItemTitle(gi.title, nullptr, /*alignFrame=*/true);
            plmActionRowBegin();
            accentButtonColors();
            if (ImGui::SmallButton("Make plan")) {
                planCall(st, "create", "task", {{"title", gi.title}});
                st.toast(ui::ToastLevel::Success, "Added a task from this GitHub issue");
            }
            ImGui::PopStyleColor(2);
            plmActionRowEnd();
            plmCardEnd();
            ImGui::PopID();
        }
    }
    ImGui::EndChild();
    ImGui::PopStyleColor();
    plmPanelEnd();

    ImGui::SameLine(0, 12 * S);

    // Milestones — title + due date + Add, all the same height and baseline.
    plmPanelBegin("##pl_ms", "Milestones", "milestone_flag", halfW);
    static char s_msTitle[160] = {};
    static char s_msDue[32] = {};
    ImGui::SetNextItemWidth(150 * S);
    ImGui::InputTextWithHint("##msTitle", "milestone", s_msTitle, sizeof(s_msTitle));
    ImGui::SameLine();
    plmDatePicker("##msDue", s_msDue, sizeof(s_msDue));
    ImGui::SameLine();
    if (plmGlassButton("##msadd", "Add", "new_doc", true) && s_msTitle[0]) {
        nlohmann::json d = {{"title", s_msTitle}};
        if (s_msDue[0]) d["due"] = s_msDue;
        planCall(st, "create", "milestone", d);
        s_msTitle[0] = '\0'; s_msDue[0] = '\0';
    }
    ImGui::Dummy(ImVec2(0, 4 * S));
    // Only the item list scrolls — the header + add-row above stay pinned.
    ImGui::PushStyleColor(ImGuiCol_ChildBg, ImVec4(0, 0, 0, 0));
    ImGui::BeginChild("##mslist", ImVec2(0, 168 * S), ImGuiChildFlags_None,
                      ImGuiWindowFlags_NavFlattened);
    if (H.milestones.empty()) dimText("No milestones yet.");
    for (const auto& m : H.milestones) drawMilestoneRow(st, m);
    ImGui::EndChild();
    ImGui::PopStyleColor();
    plmPanelEnd();

    // (Team & Sync lives on its own Team tab now.)
    ImGui::Dummy(ImVec2(0, 8 * S));

    // ---- Task board (full-width glass columns; no header — the columns label
    //      themselves, and dropping it frees vertical room for the boards) -------
    // add a task: priority picker, then a compact left-aligned title box that
    // matches the Add-task button height (no custom frame padding on either).
    // Task priority uses the SAME words as issue severity (low/med/high/critical)
    // for consistency, mapped onto the stored p3..p0 scale.
    static int s_newTaskPri = 1;                        // default med
    static const char* kPriWord[] = {"low", "med", "high", "critical"};
    static const char* kPriCode[] = {"p3", "p2", "p1", "p0"};
    ui::kit::fieldEnum("##ntpri", "Priority", &s_newTaskPri, kPriWord, IM_ARRAYSIZE(kPriWord),
                      ui::kit::FieldOpts().withEnumStyle(ui::kit::EnumStyle::Combo));
    ImGui::SetNextItemWidth(300 * S);
    bool enter = ImGui::InputTextWithHint("##newtask", "new task title\xe2\x80\xa6",
        H.newTaskTitle, sizeof(H.newTaskTitle), ImGuiInputTextFlags_EnterReturnsTrue);
    ImGui::SameLine();
    if ((plmGlassButton("##addtask", "Add task", "new_doc", true, 116 * S) || enter) &&
        H.newTaskTitle[0]) {
        planCall(st, "create", "task",
                 {{"title", H.newTaskTitle}, {"priority", kPriCode[s_newTaskPri]}});
        H.newTaskTitle[0] = '\0';
    }
    ImGui::Spacing();

    // kanban columns — bordered, scrollable boxes so cards stay contained inside
    // their column instead of spilling past the bottom. Each column scrolls
    // independently when it fills up; the boxes are sized to the remaining page
    // height so they feel like a real board.
    struct Col { const char* id; const char* label; };
    static const Col kCols[] = {
        {"todo", "To do"}, {"doing", "Doing"}, {"done", "Done"}, {"blocked", "Blocked"},
    };
    static const char* kPipe[] = {"todo", "doing", "done"};  // ordered lane; "blocked" is aside
    const float gap  = 10 * S;
    // Floor the width and shave a pixel so 4 columns + 3 gaps never exceed the
    // available width (sub-pixel overflow would wrap the 4th column onto a new row).
    float colW = std::floor((ImGui::GetContentRegionAvail().x - gap * 3) / 4.0f) - 1.0f;
    // Reserve the column-label line (drawn above each box) so label + box fit the
    // page exactly — otherwise the box bottoms spilled past the non-scrolling page.
    float colH = ImGui::GetContentRegionAvail().y
               - ImGui::GetTextLineHeightWithSpacing() - 6 * S;
    if (colH < 220 * S) colH = 220 * S;

    for (int ci = 0; ci < 4; ++ci) {
        const auto& col = kCols[ci];
        if (ci) ImGui::SameLine(0, gap);
        ImGui::BeginGroup();
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(palette.accent[0], palette.accent[1], palette.accent[2], 1.0f));
        ImGui::TextUnformatted(col.label);
        ImGui::PopStyleColor();

        ImGui::PushID(ci);
        ImVec2 cp0 = ImGui::GetCursorScreenPos();
        ui::glassBackground(ImGui::GetWindowDrawList(), cp0,
                            ImVec2(cp0.x + colW, cp0.y + colH), 10.0f);
        ImGui::PushStyleVar(ImGuiStyleVar_ChildRounding, 10 * S);
        ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(8 * S, 8 * S));
        ImGui::PushStyleColor(ImGuiCol_ChildBg, ImVec4(0, 0, 0, 0));
        ImGui::PushStyleColor(ImGuiCol_Border, ImVec4(0, 0, 0, 0));
        ImGui::BeginChild("##col", ImVec2(colW, colH), ImGuiChildFlags_Borders);

        // Tasks in this column, highest priority first. Sort a local pointer view —
        // never reorder H.allTasks (a shared cache other pages read).
        std::vector<const PlmTaskRow*> colTasks;
        for (const auto& t : H.allTasks)
            if (t.status == col.id) colTasks.push_back(&t);
        std::stable_sort(colTasks.begin(), colTasks.end(),
            [](const PlmTaskRow* a, const PlmTaskRow* b) {
                return plmPriorityRank(a->priority) < plmPriorityRank(b->priority);
            });

        for (const auto* tp : colTasks) {
            const auto& t = *tp;
            ImGui::PushID(t.id.c_str());
            bool sel = (s_selTask == t.id);
            plmCardBegin("##task", sel, 8.0f, 6.0f, plmTintPriority(t.priority), 0.32f);

            // Title wraps to the card width so the card auto-grows (AutoResizeY) to
            // fit a long title instead of clipping it to one line.
            plmItemTitle(t.title);
            // Priority is the card's background tint; hovering anywhere on the card
            // names it in a tooltip instead of drawing a dot over the title.
            priorityDot(t.priority);
            if (!t.assignee.empty()) dimText("@%s", t.assignee.c_str());

            // Admin controls — hidden until the card is selected (click to reveal).
            // ‹ › walk the todo→doing→done lane; Block parks the task; Del removes it.
            if (sel) {
                int idx = -1;
                for (int k = 0; k < 3; ++k) if (t.status == kPipe[k]) idx = k;
                bool blocked = (idx < 0);
                plmActionRowBegin();
                // ArrowButton draws a real triangle (font-independent — the ‹ › glyphs
                // were not in the atlas and rendered blank); the tight FramePadding
                // shrinks its square to a compact size.
                ImGui::BeginDisabled(!blocked && idx == 0);
                if (ImGui::ArrowButton("##prev", ImGuiDir_Left))
                    planCall(st, "update", "task",
                             {{"status", blocked ? "todo" : kPipe[idx - 1]}}, t.id);
                ImGui::EndDisabled();
                ImGui::SameLine();
                ImGui::BeginDisabled(!blocked && idx == 2);
                if (ImGui::ArrowButton("##next", ImGuiDir_Right))
                    planCall(st, "update", "task",
                             {{"status", blocked ? "doing" : kPipe[idx + 1]}}, t.id);
                ImGui::EndDisabled();
                ImGui::SameLine();
                if (blocked) {
                    if (ImGui::SmallButton("Unblock"))
                        planCall(st, "update", "task", {{"status", "todo"}}, t.id);
                } else if (ImGui::SmallButton("Block")) {
                    planCall(st, "update", "task", {{"status", "blocked"}}, t.id);
                }
                ImGui::SameLine();
                ImGui::PushStyleColor(ImGuiCol_Button,
                    ImVec4(palette.statusErr[0], palette.statusErr[1], palette.statusErr[2], 0.55f));
                if (ImGui::SmallButton("Del"))
                    planCall(st, "delete", "task", nullptr, t.id);
                ImGui::PopStyleColor();
                plmActionRowEnd();
            }
            if (plmCardEnd()) s_selTask = sel ? "" : t.id;
            ImGui::PopID();
        }
        if (colTasks.empty()) dimText("Nothing here yet.");

        ImGui::EndChild();
        ImGui::PopStyleColor(2);
        ImGui::PopStyleVar(2);
        ImGui::PopID();
        ImGui::EndGroup();
    }
}

// ---- Issues -------------------------------------------------------------------

namespace {
static std::string s_selIssue;

}  // namespace

void plmDrawIssuesPage(AppState& st) {
    auto& H = g_plmHub;
    const float S = ui::uiScale;
    plmEnsurePlanning(st);

    plmSectionHeader("Issues", "issue_bug");
    ImGui::PushStyleVar(ImGuiStyleVar_FramePadding, ImVec2(6 * S, 4 * S));
    ImGui::SetNextItemWidth(-120 * S);
    bool enter = ImGui::InputTextWithHint("##newissue", "describe an issue\xe2\x80\xa6",
        H.newIssueTitle, sizeof(H.newIssueTitle), ImGuiInputTextFlags_EnterReturnsTrue);
    ImGui::PopStyleVar();
    ImGui::SameLine();
    accentButtonColors();
    if ((ImGui::Button("Log issue", ImVec2(110 * S, 0)) || enter) && H.newIssueTitle[0]) {
        planCall(st, "create", "issue", {{"title", H.newIssueTitle}});
        H.newIssueTitle[0] = '\0';
    }
    ImGui::PopStyleColor(2);
    if (!H.planErr.empty()) plmError(H.planErr);
    ImGui::Separator();
    ImGui::Spacing();

    if (H.issues.empty()) {
        plmEmptyState("[!]", "No issues logged",
                      "Track bugs and defects here, linked to the items they affect.");
        return;
    }
    for (const auto& is : H.issues) {
        ImGui::PushID(is.id.c_str());
        bool sel = (s_selIssue == is.id);
        bool closed = (is.state == "closed");
        plmRowPill(sel, sel ? 2.6f : 1.4f);
        severityBadge(is.severity);
        ImGui::SameLine();
        if (closed) {
            ImGui::PushStyleColor(ImGuiCol_Text,
                ImVec4(palette.textDisabled[0], palette.textDisabled[1],
                       palette.textDisabled[2], 1.0f));
            ImGui::Text("(closed) %s", is.title.c_str());
            ImGui::PopStyleColor();
        } else {
            ImGui::Text("%s", is.title.c_str());
        }
        if (ImGui::IsItemClicked()) s_selIssue = sel ? "" : is.id;
        if (sel) {
            ImGui::Indent(16 * S);
            if (ImGui::SmallButton(closed ? "Reopen" : "Close issue"))
                planCall(st, "update", "issue",
                         {{"state", closed ? "open" : "closed"}}, is.id);
            ImGui::SameLine();
            ImGui::PushStyleColor(ImGuiCol_Button,
                ImVec4(palette.statusErr[0], palette.statusErr[1], palette.statusErr[2], 0.5f));
            if (ImGui::SmallButton("Delete"))
                planCall(st, "delete", "issue", nullptr, is.id);
            ImGui::PopStyleColor();
            ImGui::Unindent(16 * S);
        }
        ImGui::Spacing();
        ImGui::PopID();
    }
}

// ---- Git tab ------------------------------------------------------------------
//
// A real git / GitHub client (unlike the vault pages, git vocabulary is
// deliberate here). Big code/files panel top-left (~2/3 x 2/3), a right column
// of GitHub panels, and a bottom band with changes + commit + history.

// Run a git mutation RPC, then refresh + toast. Bumps generation on success.
static void gitCall(AppState& st, const char* method, nlohmann::json extra,
                    std::string okMsg) {
    auto& H = g_plmHub;
    if (H.busy) return;
    H.busy = true;
    nlohmann::json p = pathParam(st);
    if (extra.is_object())
        for (auto it = extra.begin(); it != extra.end(); ++it) p[it.key()] = it.value();
    st.rpc.call(method, p, [&st, okMsg](const core::RpcResult& r) {
        g_plmHub.busy = false;
        std::string h;
        if (!plmOk(r, h)) { st.toast(ui::ToastLevel::Error, h); return; }
        if (!okMsg.empty()) st.toast(ui::ToastLevel::Success, okMsg);
        plmInvalidate();
    });
}

// Load one tracked file's committed content into the code viewer.
static void gitOpenFile(AppState& st, const std::string& file) {
    auto& H = g_plmHub;
    H.gitSelectedFile = file;
    H.gitFileContent = "(loading…)";
    nlohmann::json p = pathParam(st);
    p["file"] = file;
    st.rpc.call("git_show", p, [](const core::RpcResult& r) {
        auto& H = g_plmHub;
        std::string h;
        if (!plmOk(r, h)) { H.gitFileContent = "(could not read file: " + h + ")"; return; }
        H.gitFileContent = r.result.value("content", std::string());
        H.gitFileTruncated = r.result.value("truncated", false);
    });
}

void plmDrawGitPage(AppState& st) {
    auto& H = g_plmHub;
    const float S = ui::uiScale;
    const float gap = 8.0f * S;
    plmEnsureGit(st);

    // Degraded states: no git binary, or not a repo yet.
    if (H.gitMissing) {
        plmSectionHeader("Git", "git");
        plmEmptyState("[ x ]", "Git is not installed",
                      "Install git to track this project's code here.");
        return;
    }
    if (!H.gitIsRepo && !H.gitLoading) {
        plmSectionHeader("Git", "git");
        plmEmptyState("[ + ]", "Not a git repository",
                      "This folder isn't tracked by git yet.");
        const float bw = 220 * S;
        ImGui::SetCursorPosX((ImGui::GetContentRegionAvail().x - bw) * 0.5f);
        if (!H.busy && plmGlassButton("##gitinit", "Initialize repository", "git", true, bw))
            gitCall(st, "git_init", nullptr, "Initialized a git repository");
        return;
    }

    // Outer breathing room against the page's own edges — the fill-width/height
    // panels below butt up against these on all four sides, which is what makes
    // them read as flush-against-the-window instead of properly inset.
    const float pageL = 6 * S, pageR = 6 * S, pageT = 6 * S, pageB = 6 * S;
    ImGui::Dummy(ImVec2(0, pageT));
    ImGui::SetCursorPosX(ImGui::GetCursorPosX() + pageL);
    const ImVec2 avail0 = ImGui::GetContentRegionAvail();
    const ImVec2 avail  = ImVec2(avail0.x - pageR, avail0.y - pageB);
    const float topH    = avail.y * 0.62f;                 // main band = top ~2/3
    const float bottomH = avail.y - topH - gap;            // changes + history
    // Right (Repository / Pull requests) column: ~28% of the page, but never so
    // narrow that each repository fact has to wrap mid-phrase (it only gets a DPI-
    // scaled floor), and never more than 40% so the code browser keeps the lead.
    const float sideW   = std::min(std::max(avail.x * 0.28f, 240.0f * S), avail.x * 0.40f);
    const float mainW   = avail.x - sideW - gap;           // code panel = the rest

    // Fixed-height frosted-glass panel (unlike plmPanelBegin's AutoResizeY, so fill-
    // height scroll children inside — History, the code viewer — actually get height).
    // Outer panels never scroll — they scale to their given size. Only the inner
    // content views (file list, code, branch/PR lists, changes, history log) carry
    // scrollbars for their overflow.
    auto gpanel = [&](const char* id, const char* title, const char* icon, ImVec2 sz) {
        ui::pushGlassChildStyle(10.0f);
        ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(13 * S, 6 * S));
        ImGui::BeginChild(id, sz, ImGuiChildFlags_Borders,
                          ImGuiWindowFlags_NoScrollbar | ImGuiWindowFlags_NoScrollWithMouse);
        if (title) { plmSectionHeader(title, icon); ImGui::Dummy(ImVec2(0, 1 * S)); }
    };
    auto gpanelEnd = [&]() {
        ImGui::EndChild();
        ImGui::PopStyleVar();       // WindowPadding
        ui::popGlassChildStyle();
    };

    // ---- top band: code browser (left 2/3) + GitHub column (right 1/3) --------
    const ImVec2 bandTop = ImGui::GetCursorScreenPos();  // top-left of the whole top band
    gpanel("##git_main", nullptr, nullptr, ImVec2(mainW, topH));  // no title — stretches to top
    {
        const float innerH  = ImGui::GetContentRegionAvail().y;
        const float listW   = ImGui::GetContentRegionAvail().x * 0.36f;
        const ImVec2 panelTop = ImGui::GetCursorScreenPos();  // top of the file-explorer column

        // Icon+"Code" header above the file tree — the same left-aligned
        // plmSectionHeader every other Git panel (Repository, Pull requests,
        // Changes, History) opens with, plus the same 1dp gap gpanel leaves.
        plmSectionHeader("Code", "body_folder");
        ImGui::Dummy(ImVec2(0, 1 * S));

        // File/folder tree: a real explorer with folder + file icons and glass row
        // pills. Built fresh each frame from the flat tracked-file list. It takes
        // whatever column height the header left.
        const float headerH = ImGui::GetCursorScreenPos().y - panelTop.y;
        ui::pushGlassChildStyle(8.0f);
        ImGui::BeginChild("##git_files", ImVec2(listW, std::max(1.0f, innerH - headerH)),
                          ImGuiChildFlags_Borders);
        ui::popGlassChildStyle();
        if (H.gitTree.empty()) {
            dimText(H.gitDetailsLoading ? "Loading\xe2\x80\xa6" : "No tracked files yet.");
        } else {
            struct Node { std::map<std::string, Node> dirs; std::vector<std::string> files; };
            static Node root;
            static unsigned long long treeRevision = 0;
            if (treeRevision != H.gitTreeRevision) {
                root = Node{};
                for (const auto& path : H.gitTree) {
                    Node* cur = &root;
                    size_t start = 0;
                    for (;;) {
                        size_t slash = path.find('/', start);
                        if (slash == std::string::npos) { cur->files.push_back(path.substr(start)); break; }
                        cur = &cur->dirs[path.substr(start, slash - start)];
                        start = slash + 1;
                    }
                }
                treeRevision = H.gitTreeRevision;
            }
            static std::set<std::string> s_open;   // expanded folders (full path + '/')
            // Folders and files each carry one consistent hue — folders yellow-green,
            // files blue — so the two kinds read apart at a glance (both icon and row
            // wash). Icons still shrink with nesting depth so depth stays legible.
            static const float kFolderCol[4] = {0.560f, 0.780f, 0.360f, 1.0f}; // yellow-green
            static const float kFileCol[4]   = {0.380f, 0.600f, 0.980f, 1.0f}; // blue
            auto iconSzForDepth = [&](int depth) {
                return std::max(9.0f * S, 15.0f * S - depth * 1.6f * S);
            };
            std::function<void(const Node&, const std::string&, int)> draw =
                [&](const Node& n, const std::string& prefix, int depth) {
                for (const auto& kv : n.dirs) {
                    const std::string full = prefix + kv.first + "/";
                    bool open = s_open.count(full) > 0;
                    ImGui::PushID(full.c_str());
                    const float rowH = ImGui::GetTextLineHeight() + 6 * S;
                    const float w = ImGui::GetContentRegionAvail().x;
                    ImVec2 p0 = ImGui::GetCursorScreenPos();
                    if (ImGui::InvisibleButton("##d", ImVec2(w, rowH))) {
                        if (open) s_open.erase(full); else s_open.insert(full);
                        open = !open;
                    }
                    ImDrawList* dl = ImGui::GetWindowDrawList();
                    // Each row's pill starts at its own indent so the wash steps
                    // inward with nesting — depth reads at a glance instead of every
                    // background spanning the full column width. Folders keep the
                    // accent wash; it darkens on hover.
                    const float indent = depth * 13 * S;
                    ui::glassCardWash(dl, ImVec2(p0.x + indent, p0.y), ImVec2(p0.x + w, p0.y + rowH), 5 * S,
                                      kFolderCol, ImGui::IsItemHovered() ? 0.26f : 0.13f, 0.3f);
                    float x = p0.x + indent + 4 * S;
                    const float cy = p0.y + rowH * 0.5f;
                    const ImU32 chev = Palette::toU32(palette.textDim);
                    if (open) dl->AddTriangleFilled(ImVec2(x, cy - 3 * S), ImVec2(x + 6 * S, cy - 3 * S), ImVec2(x + 3 * S, cy + 3 * S), chev);
                    else      dl->AddTriangleFilled(ImVec2(x, cy - 3 * S), ImVec2(x + 6 * S, cy), ImVec2(x, cy + 3 * S), chev);
                    x += 11 * S;
                    // Icon centered horizontally and vertically in its box; size and
                    // color both step with nesting depth.
                    const float iconSz = iconSzForDepth(depth);
                    ui::drawIconGlass(dl, "body_folder", ImVec2(x + iconSz * 0.5f, cy), iconSz, Palette::toU32(kFolderCol), 0);
                    x += iconSz + 8 * S;
                    dl->AddText(ImVec2(x, p0.y + (rowH - ImGui::GetTextLineHeight()) * 0.5f), Palette::toU32(palette.text), kv.first.c_str());
                    ImGui::PopID();
                    if (open) draw(kv.second, full, depth + 1);
                }
                for (const auto& name : n.files) {
                    const std::string full = prefix + name;
                    const bool sel = (H.gitSelectedFile == full);
                    ImGui::PushID(full.c_str());
                    const float rowH = ImGui::GetTextLineHeight() + 6 * S;
                    const float w = ImGui::GetContentRegionAvail().x;
                    ImVec2 p0 = ImGui::GetCursorScreenPos();
                    if (ImGui::InvisibleButton("##f", ImVec2(w, rowH))) gitOpenFile(st, full);
                    ImDrawList* dl = ImGui::GetWindowDrawList();
                    // Files sit one gutter further in than their folder (past the
                    // chevron), so their pill is inset further still — the hierarchy
                    // is legible from the backgrounds alone. Files carry the blue wash
                    // (vs folders' yellow-green); selected files go accent.
                    const float fa = sel ? 0.38f : (ImGui::IsItemHovered() ? 0.26f : 0.14f);
                    const float indent = depth * 13 * S + 11 * S;  // align past the chevron gutter
                    ui::glassCardWash(dl, ImVec2(p0.x + indent, p0.y), ImVec2(p0.x + w, p0.y + rowH), 5 * S,
                                      sel ? palette.accent : kFileCol, fa, 0.3f);
                    float x = p0.x + indent + 4 * S;
                    const float iconSz = iconSzForDepth(depth);
                    const ImU32 ic = sel ? Palette::toU32(palette.accent) : Palette::toU32(kFileCol);
                    // Icon centered horizontally and vertically in its box; size and
                    // color both step with nesting depth (selected files stay accent).
                    ui::drawIconGlass(dl, "new_doc", ImVec2(x + iconSz * 0.5f, p0.y + rowH * 0.5f), iconSz, ic, 0);
                    x += iconSz + 8 * S;
                    dl->AddText(ImVec2(x, p0.y + (rowH - ImGui::GetTextLineHeight()) * 0.5f),
                                sel ? Palette::toU32(palette.text) : Palette::toU32(palette.textDim), name.c_str());
                    ImGui::PopID();
                }
            };
            draw(root, "", 0);
            // Auto-open the first README if nothing is selected yet
            if (H.gitSelectedFile.empty()) {
                for (const auto& name : root.files) {
                    if (name.find("README") == 0 || name.find("readme") == 0) {
                        gitOpenFile(st, name);
                        break;
                    }
                }
            }
        }
        ImGui::EndChild();

        // File content viewer — spans the full column height (from the top of the
        // file explorer down to its bottom), not just alongside the tree below the
        // "Code" header, so top/bottom padding match.
        ImGui::SetCursorScreenPos(ImVec2(panelTop.x + listW + gap, panelTop.y));
        ui::pushGlassChildStyle(8.0f);
        ImGui::BeginChild("##git_code", ImVec2(0, innerH), ImGuiChildFlags_Borders,
                          ImGuiWindowFlags_HorizontalScrollbar);
        ui::popGlassChildStyle();
        if (H.gitSelectedFile.empty()) {
            dimText("Select a file to view its contents.");
        } else {
            ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(palette.accent[0],
                palette.accent[1], palette.accent[2], 1.0f));
            ImGui::TextUnformatted(H.gitSelectedFile.c_str());
            ImGui::PopStyleColor();
            if (H.gitFileTruncated) { ImGui::SameLine(); dimText("(truncated)"); }
            ImGui::Separator();
            ImGui::TextUnformatted(H.gitFileContent.c_str());
        }
        ImGui::EndChild();
    }
    gpanelEnd();

    // ---- right column: Repository / Pull requests ----------------------------
    // One column host, exactly the band's height, holding both panels. When they
    // fit, Pull requests stretches so its bottom border lands on the Code box's
    // bottom border; when the Repository facts are tall (narrow column, large UI
    // scale) Pull requests keeps a floor of a header + a few rows and the column
    // scrolls instead of shrinking that panel until its content is cut mid-line.
    const float rightX  = bandTop.x + mainW + gap;
    const float repoPad = 4 * S;

    ImGui::SetCursorScreenPos(ImVec2(rightX, bandTop.y));
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(0, 0));
    ImGui::BeginChild("##git_side", ImVec2(sideW, topH), ImGuiChildFlags_None,
                      ImGuiWindowFlags_NoBackground);
    ImGui::PopStyleVar();
    const float colTop = ImGui::GetCursorScreenPos().y;
    const float colX   = ImGui::GetCursorScreenPos().x;

    // Repository — drawn inline (tighter padding + shorter header than gpanel), and
    // AutoResizeY so it fits its content exactly; auto-height can't clip regardless
    // of how the facts wrap. Each fact is a pair shown on one line when it fits and
    // split at its own seam otherwise, so nothing breaks mid-phrase ("7 in / the
    // last 24h").
    auto factLine = [&](const std::string& a, const std::string& b, const char* sep) {
        const std::string joined = a + sep + b;
        if (ImGui::CalcTextSize(joined.c_str()).x <= ImGui::GetContentRegionAvail().x) {
            dimText("%s", joined.c_str());
        } else {
            dimText("%s", a.c_str());
            dimText("%s", b.c_str());
        }
    };
    ui::pushGlassChildStyle(10.0f);
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(13 * S, repoPad));
    ImGui::BeginChild("##git_repo", ImVec2(0, 0),
                      ImGuiChildFlags_Borders | ImGuiChildFlags_AutoResizeY,
                      ImGuiWindowFlags_NoScrollbar | ImGuiWindowFlags_NoScrollWithMouse);
    plmSectionHeader("Repository", "git");
    ImGui::Dummy(ImVec2(0, 2 * S));
    if (!H.gitBranch.empty()) {
        ImGui::AlignTextToFramePadding();
        dimText("On");
        ImGui::SameLine();
        ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(palette.accent[0],
            palette.accent[1], palette.accent[2], 1.0f));
        ImGui::TextUnformatted(H.gitBranch.c_str());
        ImGui::PopStyleColor();
    }
    const char* kDot = "  \xc2\xb7  ";
    if (!H.gitUpstream.empty())
        factLine(H.gitUpstream,
                 std::to_string(H.gitAhead) + " ahead, " + std::to_string(H.gitBehind) + " behind",
                 kDot);
    if (!H.ghRepoName.empty()) {
        const std::string repo = H.ghRepoOwner + "/" + H.ghRepoName;
        if (H.ghRepoVis.empty()) dimText("%s", repo.c_str());
        else                     factLine(repo, "(" + H.ghRepoVis + ")", "  ");
    }
    factLine(std::to_string(H.gitTotalCommits) + " commits",
             std::to_string(H.gitCommits24h) + " in the last 24h", kDot);
    ImGui::Dummy(ImVec2(0, 3 * S));
    if (H.ghAuthed) {
        factLine("Signed in as", H.ghUser, " ");
    } else {
        if (!H.busy && plmGlassButton("##ghsignin", "Sign in with GitHub", "team", true))
            st.rpc.call("git_gh_auth_login", pathParam(st),
                        [&st](const core::RpcResult& r) {
                std::string h;
                if (!plmOk(r, h)) { st.toast(ui::ToastLevel::Error, h); return; }
                g_plmHub.ghLoginCode = r.result.value("code", std::string());
                g_plmHub.ghLoginUrl  = r.result.value("url", std::string());
                st.toast(ui::ToastLevel::Info, "Opening your browser to sign in\xe2\x80\xa6");
            });
        if (!H.ghLoginCode.empty()) {
            ImGui::TextUnformatted(H.ghLoginCode.c_str());
            dimText("%s", H.ghLoginUrl.c_str());
        }
    }
    ImGui::EndChild();
    const float repoBottom = ImGui::GetItemRectMax().y;  // actual auto-sized bottom
    ImGui::PopStyleVar();       // WindowPadding
    ui::popGlassChildStyle();

    // Pull requests — directly under the auto-sized Repository box, filling the
    // rest of the band (flush with the Code box bottom) but never shorter than its
    // header plus a few list rows. (GitHub issues live on the Planning tab.)
    const float prTop = repoBottom + gap;
    const float prMin = ImGui::GetFrameHeightWithSpacing() * 2.0f +
                        ImGui::GetTextLineHeightWithSpacing() * 3.0f;
    const float prH   = std::max((colTop + topH) - prTop, prMin);
    ImGui::SetCursorScreenPos(ImVec2(colX, prTop));
    gpanel("##git_prs", "Pull requests", "plan_board", ImVec2(0, prH));
    ImGui::BeginChild("##git_prlist", ImVec2(0, 0), ImGuiChildFlags_None);
    if (H.gitPrs.empty()) dimText(H.ghAuthed ? "No open pull requests." : "Sign in to see pull requests.");
    for (const auto& pr : H.gitPrs) dimText("#%d  %s", pr.number, pr.title.c_str());
    ImGui::EndChild();
    gpanelEnd();
    ImGui::EndChild();  // ##git_side

    // Return the cursor to the band's bottom-left so the bottom band flows below.
    ImGui::SetCursorScreenPos(ImVec2(bandTop.x, bandTop.y + topH));
    ImGui::Dummy(ImVec2(0, gap));

    // ---- bottom band: Changes + commit (left) | History (right) ---------------
    // Re-apply the page's left inset — the bottom band starts a fresh line, whose
    // cursor resets to x=0, so without this the Changes panel's left edge sits
    // pageL short of the Code box above it instead of flush under it.
    ImGui::SetCursorPosX(ImGui::GetCursorPosX() + pageL);
    const float halfW = (avail.x - gap) * 0.5f;
    gpanel("##git_changes", "Changes", "activity", ImVec2(halfW, bottomH));
    {
        // Reserve the commit box + button row at the bottom; the change list fills
        // the rest and scrolls.
        const float commitH = ImGui::GetFrameHeight() * 2 + 14 * S;
        const float listH = std::max(30.0f * S, ImGui::GetContentRegionAvail().y - commitH);
        ImGui::BeginChild("##git_changed", ImVec2(0, listH), ImGuiChildFlags_None);
        if (H.gitClean) {
            dimText("Working tree clean — nothing to commit.");
        } else {
            for (const auto& f : H.gitFiles) {
                ImGui::PushID(f.path.c_str());
                bool staged = f.staged;
                if (ImGui::Checkbox("##stage", &staged))
                    gitCall(st, staged ? "git_stage" : "git_unstage",
                            {{"files", nlohmann::json::array({f.path})}}, "");
                ImGui::SameLine();
                const float* col = f.untracked ? palette.accent
                                 : f.staged ? palette.statusOk : palette.statusWarn;
                ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(col[0], col[1], col[2], 0.95f));
                ImGui::AlignTextToFramePadding();
                ImGui::TextUnformatted(f.untracked ? "A" : f.staged ? "M" : "M");
                ImGui::PopStyleColor();
                ImGui::SameLine();
                ImGui::AlignTextToFramePadding();
                ImGui::TextUnformatted(f.path.c_str());
                ImGui::PopID();
            }
        }
        ImGui::EndChild();
        ImGui::SetNextItemWidth(-1);
        ImGui::InputTextWithHint("##commitmsg", "commit message\xe2\x80\xa6",
                                 H.gitCommitMsg, sizeof(H.gitCommitMsg));
        ImGui::Dummy(ImVec2(0, 2 * S));
        if (!H.busy && plmGlassButton("##commit", "Commit", "save", true)) {
            if (H.gitCommitMsg[0]) {
                gitCall(st, "git_commit",
                        {{"message", std::string(H.gitCommitMsg)}, {"stage_all", false}},
                        "Committed");
                H.gitCommitMsg[0] = '\0';
            } else {
                st.toast(ui::ToastLevel::Error, "Enter a commit message first");
            }
        }
        ImGui::SameLine();
        if (!H.busy && plmGlassButton("##push", "Push", nullptr))
            gitCall(st, "git_push", {{"set_upstream", H.gitUpstream.empty()}}, "Pushed");
        ImGui::SameLine();
        if (!H.busy && plmGlassButton("##pull", "Pull", nullptr))
            gitCall(st, "git_pull", nullptr, "Pulled");
        ImGui::SameLine();
        if (!H.busy && plmGlassButton("##fetch", "Fetch", nullptr))
            gitCall(st, "git_fetch", nullptr, "Fetched");
    }
    gpanelEnd();

    ImGui::SameLine(0, gap);

    gpanel("##git_hist", "History", "history", ImVec2(avail.x - halfW - gap, bottomH));
    {
        ImGui::BeginChild("##git_log", ImVec2(0, 0), ImGuiChildFlags_None);
        if (H.gitLog.empty()) dimText(H.gitLoading ? "Loading\xe2\x80\xa6" : "No commits yet.");
        for (const auto& c : H.gitLog) {
            ImGui::PushID(c.hash.c_str());
            plmCardBegin("##gc", false, 9.0f, 5.0f, palette.accent, 0.13f);
            plmItemTitle(c.subject);          // wraps long commit subjects
            dimText("%s  \xc2\xb7  %s  \xc2\xb7  %s", c.hash.c_str(),
                    c.author.c_str(), c.date.c_str());
            plmCardEnd();
            ImGui::PopID();
        }
        ImGui::EndChild();
    }
    gpanelEnd();
}

}  // namespace ui
