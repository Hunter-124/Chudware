// PLM Project Hub — page bodies.
//
// Overview (dashboard + quick checkpoint) and Files (items grid + track/link) are
// implemented here; History/Releases/Planning/Issues/Team/Settings are staged as
// honest placeholders filled in later phases. Every page reads only the cached
// rows on g_plmHub (filled by plm_state.cpp) and mutates via async st.rpc; on a
// successful mutation it bumps the generation so caches refetch.
#include "plm_hub.h"

#include <algorithm>
#include <cstdarg>
#include <cstdio>
#include <cstring>
#include <ctime>
#include <string>
#include <vector>

#include "imgui.h"

#include "core/app_state.h"
#include "core/async_rpc.h"
#include "ui/file_dialog.h"
#include "ui/icons.h"
#include "ui_theme.h"

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
const float* plmTintPriority(const std::string& pr) {
    if (pr == "p0") return palette.statusErr;    // critical
    if (pr == "p1") return palette.statusWarn;   // high
    if (pr == "p3") return palette.textDisabled; // low
    return palette.accent;                        // p2 / med / default
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

// A self-bounding "card": an auto-height bordered child whose background is a
// priority-coloured liquid-glass gradient (frosted base + a vertical colour wash,
// brighter at the top). Because AutoResizeY means the height isn't known until the
// content is laid out, the gradient is painted in plmCardEnd on a *background*
// draw-channel (channel 0, under the content on channel 1) once the size is final.
// `tint` (a palette float[4]) selects the wash colour; nullptr = neutral.
struct PlmCardStyle { float r, g, b, a; };
static std::vector<PlmCardStyle> s_cardStyleStack;

void plmCardBegin(const char* id, bool sel, float padX = 8.0f, float padY = 6.0f,
                  const float* tint = nullptr, float tintA = 0.20f) {
    const float S = ui::uiScale;
    const float* tc = sel ? palette.accent : (tint ? tint : palette.text);
    // Transparent native body (we paint our own gradient); border echoes the tint.
    ImGui::PushStyleColor(ImGuiCol_ChildBg, ImVec4(0, 0, 0, 0));
    ImGui::PushStyleColor(ImGuiCol_Border,
        sel ? ImVec4(palette.accent[0], palette.accent[1], palette.accent[2], 0.60f)
            : ImVec4(tc[0], tc[1], tc[2], tint ? 0.40f : 0.14f));
    ImGui::PushStyleVar(ImGuiStyleVar_ChildRounding, 6.0f * S);
    ImGui::PushStyleVar(ImGuiStyleVar_ChildBorderSize, 1.0f);
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(padX * S, padY * S));
    ImGui::BeginChild(id, ImVec2(0, 0),
                      ImGuiChildFlags_AutoResizeY | ImGuiChildFlags_Borders);
    // Content draws on the foreground channel; the gradient goes behind it in End.
    // Split + merge are unconditional and paired (End always merges).
    ImGui::GetWindowDrawList()->ChannelsSplit(2);
    ImGui::GetWindowDrawList()->ChannelsSetCurrent(1);
    const float baseA = sel ? 0.28f : (tint ? tintA : 0.055f);
    s_cardStyleStack.push_back({tc[0], tc[1], tc[2], baseA});
}

// Close a card opened with plmCardBegin. Paints the deferred liquid-glass gradient
// on the background channel, merges, then returns true if the card body was clicked
// outside any inner widget (for row/card selection).
bool plmCardEnd(float gap = 3.0f) {
    const float S = ui::uiScale;
    ImDrawList* dl = ImGui::GetWindowDrawList();
    PlmCardStyle cs = s_cardStyleStack.empty()
        ? PlmCardStyle{palette.text[0], palette.text[1], palette.text[2], 0.055f}
        : s_cardStyleStack.back();
    if (!s_cardStyleStack.empty()) s_cardStyleStack.pop_back();

    ImVec2 mn = ImGui::GetWindowPos();
    ImVec2 sz = ImGui::GetWindowSize();
    ImVec2 mx(mn.x + sz.x, mn.y + sz.y);
    const float rnd = 6.0f * S;
    dl->ChannelsSetCurrent(0);                 // background, under the content
    ui::glassBackground(dl, mn, mx, 6.0f);     // frosted translucent base + shine
    const ImU32 top = ImGui::GetColorU32(ImVec4(cs.r, cs.g, cs.b, cs.a));
    const ImU32 bot = ImGui::GetColorU32(ImVec4(cs.r, cs.g, cs.b, cs.a * 0.30f));
    // The wash must cover the FULL rounded card (corners included): a rounded base
    // (owns every corner at the faint bottom colour), a rounded-top strip so the top
    // corners get the bright colour, then a full-width vertical fade in the interior.
    dl->AddRectFilled(mn, mx, bot, rnd);
    dl->AddRectFilled(mn, ImVec2(mx.x, mn.y + rnd), top, rnd, ImDrawFlags_RoundCornersTop);
    if (mx.y - rnd > mn.y + rnd)
        dl->AddRectFilledMultiColor(ImVec2(mn.x, mn.y + rnd), ImVec2(mx.x, mx.y - rnd),
                                    top, top, bot, bot);
    dl->ChannelsMerge();

    bool clicked = ImGui::IsWindowHovered() &&
                   ImGui::IsMouseClicked(ImGuiMouseButton_Left) &&
                   !ImGui::IsAnyItemHovered();
    ImGui::EndChild();
    ImGui::PopStyleVar(3);
    ImGui::PopStyleColor(2);
    ImGui::Dummy(ImVec2(0, gap * S));
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

// Shorten an ISO timestamp (2026-07-07T19:54:28Z) to a readable "2026-07-07  19:54".
std::string plmNiceTime(const std::string& iso) {
    if (iso.size() < 16 || iso[10] != 'T') return iso;
    return iso.substr(0, 10) + "  " + iso.substr(11, 5);
}

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

// A liquid-glass action button: the shared frosted-glass panel (translucent base +
// vertical shine + specular + border), an accent-tinted wash when `accent`, and an
// optional atlas icon + label centered inside. Matches the glass language used in
// the ribbon / parts hub instead of a flat ImGui::Button. Returns clicked.
bool plmGlassButton(const char* id, const char* label, const char* icon,
                    bool accent = false, float widthOverride = 0.0f) {
    const float S = ui::uiScale;
    const float iconSz  = (icon && icon[0]) ? 16 * S : 0.0f;
    const float iconGap = iconSz > 0 ? 6 * S : 0.0f;
    const float padX    = 13 * S;
    const float textW   = (label && label[0]) ? ImGui::CalcTextSize(label).x : 0.0f;
    const float content = iconSz + iconGap + textW;
    const float w = widthOverride > 0 ? widthOverride : (padX * 2 + content);
    const float h = ImGui::GetFrameHeight();   // aligns with adjacent input rows

    ImVec2 p0 = ImGui::GetCursorScreenPos();
    ImVec2 p1(p0.x + w, p0.y + h);
    bool clicked = ImGui::InvisibleButton(id, ImVec2(w, h));
    bool hov = ImGui::IsItemHovered(), held = ImGui::IsItemActive();
    ImDrawList* dl = ImGui::GetWindowDrawList();

    ui::glassBackground(dl, p0, p1, 9.0f);
    if (accent) {
        float a = held ? 0.92f : hov ? 0.80f : 0.66f;
        dl->AddRectFilled(p0, p1, ImGui::GetColorU32(ImVec4(
            palette.accent[0], palette.accent[1], palette.accent[2], a)), 9.0f * S);
    } else if (hov) {
        dl->AddRectFilled(p0, p1, ImGui::GetColorU32(ImVec4(
            palette.text[0], palette.text[1], palette.text[2], held ? 0.10f : 0.06f)), 9.0f * S);
    }

    ImU32 tcol = accent ? IM_COL32(255, 255, 255, 255) : Palette::toU32(palette.text);
    float x = p0.x + (w - content) * 0.5f;
    if (iconSz > 0) {
        ui::drawIconGlass(dl, icon, ImVec2(x + iconSz * 0.5f, p0.y + h * 0.5f),
                          iconSz, tcol, 0);
        x += iconSz + iconGap;
    }
    if (textW > 0)
        dl->AddText(ImVec2(x, p0.y + (h - ImGui::GetTextLineHeight()) * 0.5f), tcol, label);
    return clicked;
}

void plmComingSoon(const char* title, const char* note) {
    plmSectionHeader(title);
    plmEmptyState("[ \xc2\xb7 ]", note,
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

}  // namespace

void plmDrawOverviewPage(AppState& st) {
    auto& H = g_plmHub;
    const float S = ui::uiScale;
    plmEnsureOverview(st);

    if (!H.ovErr.empty()) { plmError(H.ovErr); return; }

    const auto& ov = H.overview;

    // Quadrant bodies. Laid out 2×2:  [ Save a checkpoint | Overview ]
    //                                 [ Recent activity   | Open tasks ]
    auto bodyCheckpoint = [&]() {
        dimText("Record a point in this project's history you can return to.");
        ImGui::Dummy(ImVec2(0, 2 * S));
        ImGui::SetNextItemWidth(-1);
        bool enter = ImGui::InputTextWithHint("##plm_ckpt", "What changed? (optional)",
            H.checkpointMsg, sizeof(H.checkpointMsg), ImGuiInputTextFlags_EnterReturnsTrue);
        bool save = plmGlassButton("##ckpt_btn", "Save checkpoint", "save", true) || enter;
        if (save && !H.busy) doCheckpoint(st);
        if (H.busy) plmSpinner("Saving");
        plmError(H.actionErr);
        if (!H.actionOk.empty()) {
            ImGui::PushStyleColor(ImGuiCol_Text,
                ImVec4(palette.statusOk[0], palette.statusOk[1], palette.statusOk[2], 1.0f));
            ImGui::TextUnformatted(H.actionOk.c_str());
            ImGui::PopStyleColor();
        }
    };
    auto bodyStats = [&]() {
        struct SC { const char* label; int n; const float* col; };
        const SC scs[] = {
            {"Files",      ov.itemsCount,     palette.accent},
            {"Unsaved",    ov.dirtyCount,     ov.dirtyCount ? palette.statusWarn : palette.textDim},
            {"Tasks",      ov.openTasks,      palette.textDim},
            {"Issues",     ov.openIssues,     ov.openIssues ? palette.statusWarn : palette.textDim},
            {"Milestones", ov.openMilestones, palette.textDim},
        };
        const float cw = 86 * S, cg = 8 * S;
        int perRow = std::max(1, (int)std::floor(
            (ImGui::GetContentRegionAvail().x + cg) / (cw + cg)));
        for (int i = 0; i < (int)IM_ARRAYSIZE(scs); ++i) {
            if (i % perRow) ImGui::SameLine(0, cg);
            statCard(scs[i].label, scs[i].n, scs[i].col, cw);
        }
        if (!ov.nextMilestone.empty()) {
            ImGui::Dummy(ImVec2(0, 2 * S));
            dimText("Next milestone: %s", ov.nextMilestone.c_str());
        }
    };
    auto bodyRecent = [&]() {
        if (ov.recent.empty()) { dimText("No history yet."); return; }
        for (const auto& e : ov.recent) {
            ImGui::PushID(e.commit.c_str());
            plmCardBegin("##act", false, 8.0f, 3.0f,
                         e.kind == "release" ? palette.statusOk : palette.accent, 0.15f);
            std::string headline = !e.message.empty() ? e.message
                                 : (!e.summary.empty() ? e.summary : "(saved)");
            plmCopyText("##acth", headline);
            dimText("%s  \xc2\xb7  %s", e.author.c_str(), plmNiceTime(e.authoredAt).c_str());
            plmCardEnd();
            ImGui::PopID();
        }
    };
    auto bodyTasks = [&]() {
        if (ov.tasks.empty()) {
            dimText("No open tasks.");
        } else {
            for (const auto& t : ov.tasks) {
                ImGui::PushID(t.id.c_str());
                plmCardBegin("##ovt", false, 8.0f, 3.0f, plmTintPriority(t.priority), 0.20f);
                plmCopyText("##ovtt", t.title);
                priorityDot(t.priority);
                plmCardEnd();
                ImGui::PopID();
            }
        }
        if (ov.dirtyCount > 0) {
            ImGui::Dummy(ImVec2(0, 6 * S));
            ImGui::PushStyleColor(ImGuiCol_Text,
                ImVec4(palette.textDim[0], palette.textDim[1], palette.textDim[2], 1.0f));
            ImGui::TextUnformatted("Unsaved files");
            ImGui::PopStyleColor();
            for (const auto& it : ov.dirtyItems) {
                plmDocTypeTag(it.docType, it.kind);
                ImGui::SameLine();
                ImGui::AlignTextToFramePadding();
                ImGui::TextWrapped("%s", it.path.c_str());
            }
        }
    };

    const float halfW = (ImGui::GetContentRegionAvail().x - 12 * S) * 0.5f;

    // Row 1 — Save a checkpoint (left) · Overview (right)
    plmPanelBegin("##ov_ckpt", "Save a checkpoint", "save", halfW);
    bodyCheckpoint();
    plmPanelEnd();
    ImGui::SameLine(0, 12 * S);
    plmPanelBegin("##ov_stats", "Overview", "app", halfW);
    bodyStats();
    plmPanelEnd();

    ImGui::Dummy(ImVec2(0, 10 * S));

    // Row 2 — Recent activity (left) · Open tasks (right)
    plmPanelBegin("##ov_recent", "Recent activity", "history", halfW);
    bodyRecent();
    plmPanelEnd();
    ImGui::SameLine(0, 12 * S);
    plmPanelBegin("##ov_tasks", "Open tasks", "prompt_edit", halfW);
    bodyTasks();
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
    dimText("Link a GitHub / Git repo (version-controlled outside AI-CAD).");
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
    const float sortW = 130 * S;
    ImGui::SameLine(ImGui::GetContentRegionMax().x - sortW);
    ImGui::SetNextItemWidth(sortW);
    ImGui::Combo("##sort", &H.sortMode, kSort, IM_ARRAYSIZE(kSort));
    ImGui::Separator();
    ImGui::Spacing();

    // Only the item list scrolls; the header + track/link + filter row above stay
    // pinned at the top of the panel.
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(0, 0));
    ImGui::BeginChild("##files_list", ImVec2(0, 0), ImGuiChildFlags_None);
    ImGui::PopStyleVar();
    if (H.itemsLoading && H.items.empty()) { plmSpinner("Loading"); ImGui::EndChild(); return; }
    if (!H.itemsErr.empty()) { plmError(H.itemsErr); ImGui::EndChild(); return; }

    // build a filtered + sorted view (pointers into H.items — no copies)
    std::vector<const PlmItemRow*> view;
    view.reserve(H.items.size());
    for (const auto& it : H.items)
        if (passesFilter(it, H.kindFilter)) view.push_back(&it);
    std::stable_sort(view.begin(), view.end(),
        [&](const PlmItemRow* a, const PlmItemRow* b) {
            switch (H.sortMode) {
                case 1: return a->kind + a->docType < b->kind + b->docType;
                case 2: {  // revision, highest first
                    const std::string& ra = !a->rev.empty() ? a->rev : a->workingRev;
                    const std::string& rb = !b->rev.empty() ? b->rev : b->workingRev;
                    return ra > rb;
                }
                case 3: return a->lifecycle < b->lifecycle;
                case 4: return a->dirty > b->dirty;   // unsaved first
                default: return a->path < b->path;    // name A→Z
            }
        });

    int shown = 0;
    for (const PlmItemRow* itp : view) {
        const auto& it = *itp;
        ++shown;
        ImGui::PushID(it.itemId.c_str());
        bool sel = (H.selectedItem == it.itemId);
        const float* ftint = it.dirty ? palette.statusWarn
                           : (it.lifecycle == "Released" ? palette.statusOk : palette.accent);
        plmCardBegin("##fitem", sel, 8.0f, 4.0f, ftint, 0.13f);

        // one padded row: [TAG] path .......... rev · lifecycle  ●
        plmDocTypeTag(it.docType, it.kind);
        ImGui::SameLine();
        ImGui::AlignTextToFramePadding();
        ImGui::TextUnformatted(it.path.c_str());

        // right-pinned badge, clamped so it never crosses the text on narrow cards
        float badgeW = 130 * S + (it.dirty ? 16 * S : 0);
        float pinX = ImGui::GetContentRegionMax().x - badgeW;
        if (pinX > ImGui::GetCursorPosX() + 8 * S) ImGui::SameLine(pinX);
        else ImGui::SameLine(0, 12 * S);
        plmLifecycleBadge(it.lifecycle, !it.rev.empty() ? it.rev : it.workingRev);
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
                nlohmann::json p = pathParam(st);
                p["item_id"] = it.itemId;
                st.rpc.call("plm_where_used", p, [&st](const core::RpcResult& r) {
                    std::string h; if (!plmOk(r, h)) { st.toast(ui::ToastLevel::Error, h); return; }
                    int n = 0;
                    if (auto c = r.result.find("consumers");
                        c != r.result.end() && c->is_array()) n = (int)c->size();
                    st.toast(ui::ToastLevel::Info,
                             n ? (std::to_string(n) + " place(s) use this")
                               : "Not used elsewhere");
                });
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
    ImGui::EndChild();
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

    plmSectionHeader("History", "history");

    // scope toggle
    static const char* kScope[] = {"Design", "Planning", "All"};
    for (int i = 0; i < 3; ++i) {
        if (i) ImGui::SameLine();
        bool sel = (H.histScope == i);
        ImGui::PushStyleColor(ImGuiCol_Button, sel
            ? ImVec4(palette.accent[0], palette.accent[1], palette.accent[2], 0.30f)
            : ImVec4(palette.button[0], palette.button[1], palette.button[2], 0.5f));
        if (ImGui::SmallButton(kScope[i]) && H.histScope != i) {
            H.histScope = i;
            H.histGen = -1;  // force refetch for the new scope
        }
        ImGui::PopStyleColor();
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
    for (const auto& e : H.history) {
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
        std::string headline = !e.message.empty() ? e.message
                             : (!e.summary.empty() ? e.summary : "(saved)");
        plmCopyText("##hh", headline);
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

    plmSectionHeader("Releases", "ok_badge");
    dimText("Stamp the next revision and freeze the design.");
    ImGui::Spacing();

    // Header stays pinned; the release list scrolls below.
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(0, 0));
    ImGui::BeginChild("##rel_scroll", ImVec2(0, 0), ImGuiChildFlags_None);
    ImGui::PopStyleVar();
    if (!H.itemsErr.empty()) { plmError(H.itemsErr); ImGui::EndChild(); return; }
    if (H.items.empty()) {
        plmEmptyState("(#)", "Nothing to release yet",
                      "Add a design or document, then release it here.");
        ImGui::EndChild();
        return;
    }

    for (const auto& it : H.items) {
        ImGui::PushID(it.itemId.c_str());
        bool open = (s_releaseItem == it.itemId);
        plmCardBegin("##rel", open, 8.0f, 6.0f,
                     it.lifecycle == "Released" ? palette.statusOk : palette.accent, 0.13f);
        plmDocTypeTag(it.docType, it.kind);
        ImGui::SameLine();
        ImGui::AlignTextToFramePadding();
        ImGui::TextUnformatted(it.path.c_str());

        // right cluster: [rev · lifecycle]  [Release… | released] — clamp so the
        // controls never overrun the path text in a narrow embedded card.
        const float ctrlW = 200 * S;
        float pinX = ImGui::GetContentRegionMax().x - ctrlW;
        bool tight = pinX <= ImGui::GetCursorPosX() + 8 * S;
        if (tight) { ImGui::NewLine(); } else { ImGui::SameLine(pinX); }
        plmLifecycleBadge(it.lifecycle, !it.rev.empty() ? it.rev : it.workingRev);
        ImGui::SameLine(0, 12 * S);
        if (it.lifecycle == "Released") {
            ImGui::AlignTextToFramePadding();
            dimText("released");
        } else if (ImGui::SmallButton(open ? "Cancel" : "Release\xe2\x80\xa6")) {
            s_releaseItem = open ? "" : it.itemId;
            s_releaseNote[0] = '\0';
            H.actionErr.clear();
        }
        if (open) {
            ImGui::Indent(16 * S);
            ImGui::PushStyleVar(ImGuiStyleVar_FramePadding, ImVec2(6 * S, 4 * S));
            ImGui::SetNextItemWidth(-1);
            ImGui::InputTextWithHint("##relnote", "release note (what this revision is)",
                                     s_releaseNote, sizeof(s_releaseNote));
            ImGui::PopStyleVar();
            accentButtonColors();
            if (!H.busy && ImGui::Button("Release", ImVec2(120 * S, 0)))
                doRelease(st, it.itemId, false);
            ImGui::PopStyleColor(2);
            if (!H.actionErr.empty()) {
                plmError(H.actionErr);
                ImGui::PushStyleColor(ImGuiCol_Button,
                    ImVec4(palette.statusWarn[0], palette.statusWarn[1], palette.statusWarn[2], 0.6f));
                if (!H.busy && ImGui::Button("Release anyway", ImVec2(140 * S, 0)))
                    doRelease(st, it.itemId, true);
                ImGui::PopStyleColor();
            }
            if (H.busy) plmSpinner("Releasing");
            ImGui::Unindent(16 * S);
        }
        plmCardEnd();
        ImGui::PopID();
    }
    ImGui::EndChild();
}

// ---- Team & Sync --------------------------------------------------------------

void plmDrawTeamPage(AppState& st) {
    (void)st;
    plmSectionHeader("Team & Sync", "team");
    dimText("Share a project through a shared folder or your own server. Everyone "
            "keeps working; Sync brings changes together without ever merging or "
            "losing work — if two people change the same design, both versions are "
            "kept.");
    ImGui::Spacing();
    plmSectionHeader("Shared locations", "vendor_globe");
    dimText("No shared location connected yet.");
    ImGui::Spacing();
    ImGui::BeginDisabled(true);
    ImGui::Button("Connect a shared folder\xe2\x80\xa6");
    ImGui::SameLine();
    ImGui::Button("Sync now");
    ImGui::EndDisabled();
    ImGui::Spacing();
    dimText("Sharing and Sync arrive in a later update — the project is fully "
            "usable on this machine today.");
}

// ---- Settings -----------------------------------------------------------------

void plmDrawSettingsPage(AppState& st) {
    auto& H = g_plmHub;
    const float S = ui::uiScale;
    plmEnsureSettings(st);

    plmSectionHeader("Settings", "settings");
    if (!H.setErr.empty()) { plmError(H.setErr); return; }

    // ---- Category grid nav — one glass cell per settings group, so the page reads
    //      as a tidy switcher instead of a long scroll and scales as groups are
    //      added for bigger teams. Only one group's panel shows at a time. --------
    struct SetCat { const char* label; const char* icon; };
    static const SetCat kSetCats[] = {
        {"Part numbers", "new_doc"}, {"AI assistance", "health_check"},
        {"Organization", "settings"}, {"Team & Sync", "team"},
    };
    static int s_setCat = 0;
    {
        const int   cols = 4;
        const float gg   = 8 * S;
        const float gw   = ImGui::GetContentRegionAvail().x;
        const float cw   = std::floor((gw - gg * (cols - 1)) / cols);
        ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing, ImVec2(gg, gg));
        for (int i = 0; i < (int)IM_ARRAYSIZE(kSetCats); ++i) {
            if (i % cols) ImGui::SameLine();
            char cid[16]; std::snprintf(cid, sizeof(cid), "##setcat%d", i);
            if (plmGlassButton(cid, kSetCats[i].label, kSetCats[i].icon, s_setCat == i, cw))
                s_setCat = i;
        }
        ImGui::PopStyleVar();
    }
    ImGui::Dummy(ImVec2(0, 10 * S));

    // part-number scheme
    if (s_setCat == 0) {
    plmPanelBegin("##set_pn", "Part-number scheme", "new_doc");
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
    }

    // AI assistance
    if (s_setCat == 1) {
    plmPanelBegin("##set_ai", "AI assistance", "health_check");
    dimText("How much the assistant helps with history. Manual controls are always "
            "available.");
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
    plmPanelEnd();
    }

    // ---- Organization & access (preview — applies once a team is connected) ----
    if (s_setCat == 2) {
    plmPanelBegin("##set_org", "Organization & access", "settings");
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
    }

    // ---- Team & Sync (moved here from the Planning page) ----
    if (s_setCat == 3) {
    plmPanelBegin("##set_team", "Team & Sync", "team");
    dimText("Share a project through a shared folder or your own server. Everyone "
            "keeps working; Sync brings changes together without ever merging or "
            "losing work — if two people change the same design, both versions are "
            "kept.");
    ImGui::Dummy(ImVec2(0, 6 * S));
    ImGui::TextUnformatted("Shared locations");
    dimText("No shared location connected yet.");
    ImGui::Dummy(ImVec2(0, 4 * S));
    ImGui::BeginDisabled(true);
    ImGui::Button("Connect a shared folder\xe2\x80\xa6");
    ImGui::SameLine();
    ImGui::Button("Sync now");
    ImGui::EndDisabled();
    ImGui::Dummy(ImVec2(0, 2 * S));
    dimText("Sharing and Sync arrive in a later update — the project is fully "
            "usable on this machine today.");
    plmPanelEnd();
    }
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
    std::time_t tt = std::time(nullptr);
    std::tm now{};
#if defined(_WIN32)
    localtime_s(&now, &tt);
#else
    localtime_r(&tt, &now);
#endif
    if (!haveVal) { y = now.tm_year + 1900; mo = now.tm_mon + 1; d = 0; }

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
            bool isToday = (vy == now.tm_year + 1900 && vm == now.tm_mon + 1 &&
                            day == now.tm_mday);
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

// A small filled priority dot, right-aligned to the card's bottom-right corner.
// p0 = red, p1 = orange/warn, p2 = neutral dim, p3 = faint. Hover shows the label.
void priorityDot(const std::string& pr) {
    const float S = ui::uiScale;
    std::string p = pr.empty() ? "p2" : pr;
    const float* c = palette.textDim;
    float a = 0.9f;
    if (p == "p0")      { c = palette.statusErr;  }
    else if (p == "p1") { c = palette.statusWarn; }
    else if (p == "p3") { c = palette.textDisabled; a = 0.7f; }
    // Draw a real filled circle — the ● glyph isn't in the font atlas (rendered
    // as a '?'), so paint it directly on the draw-list.
    const float r = 4.5f * S;
    const float rowH = ImGui::GetFrameHeight();   // match the copy-text field height
    ImGui::SameLine(ImGui::GetContentRegionMax().x - 14 * S);
    ImVec2 o = ImGui::GetCursorScreenPos();
    ImGui::Dummy(ImVec2(r * 2, rowH));
    ImVec2 ctr(o.x + r, o.y + rowH * 0.5f);
    ImGui::GetWindowDrawList()->AddCircleFilled(
        ctr, r, ImGui::GetColorU32(ImVec4(c[0], c[1], c[2], a)), 16);
    if (ImGui::IsItemHovered()) {
        const char* word = p == "p0" ? "critical" : p == "p1" ? "high"
                         : p == "p3" ? "low" : "med";
        ImGui::SetTooltip("priority: %s", word);
    }
}

static std::string s_selTask;

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
        ImGui::PushStyleColor(ImGuiCol_Text, done
            ? ImVec4(palette.statusOk[0], palette.statusOk[1], palette.statusOk[2], 1.0f)
            : ImVec4(palette.accent[0], palette.accent[1], palette.accent[2], 1.0f));
        ImGui::AlignTextToFramePadding();
        ImGui::TextUnformatted(done ? "Done" : "Open");
        ImGui::PopStyleColor();
        ImGui::SameLine();
        // Title wraps so a long name grows the card instead of clipping.
        ImGui::PushTextWrapPos(0.0f);
        ImGui::AlignTextToFramePadding();
        ImGui::TextUnformatted(m.title.c_str());
        ImGui::PopTextWrapPos();
        // action row (due date rides inline on the right so the row stays compact)
        ImGui::PushStyleVar(ImGuiStyleVar_FramePadding, ImVec2(4 * S, 1 * S));
        ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing, ImVec2(3 * S, 3 * S));
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
        ImGui::PopStyleVar(2);
    }
    plmCardEnd();
    ImGui::PopID();
}

// One issue row on the Planning page: severity + title, with visible actions —
// including "Make plan", which spins the issue into a linked task so you can
// organise fixes right where you see the problems.
static void drawIssueRow(AppState& st, const PlmIssueRow& is) {
    const float S = ui::uiScale;
    bool closed = (is.state == "closed");
    ImGui::PushID(is.id.c_str());
    plmCardBegin("##is", false, 8.0f, 3.0f, plmTintSeverity(is.severity), 0.16f);
    const float* sc = palette.textDim;
    if (is.severity == "critical" || is.severity == "high") sc = palette.statusErr;
    else if (is.severity == "med") sc = palette.statusWarn;
    ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(sc[0], sc[1], sc[2], 1.0f));
    ImGui::AlignTextToFramePadding();
    ImGui::Text("[%s]", is.severity.empty() ? "med" : is.severity.c_str());
    ImGui::PopStyleColor();
    ImGui::SameLine();
    if (closed) {
        plmCopyText("##ist", "(closed) " + is.title, palette.textDisabled);
    } else {
        plmCopyText("##ist", is.title);
    }
    ImGui::PushStyleVar(ImGuiStyleVar_FramePadding, ImVec2(4 * S, 1 * S));
    ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing, ImVec2(3 * S, 3 * S));
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
    ImGui::PopStyleVar(2);
    plmCardEnd();
    ImGui::PopID();
}

void plmDrawPlanningPage(AppState& st) {
    auto& H = g_plmHub;
    const float S = ui::uiScale;
    plmEnsurePlanning(st);

    if (!H.planErr.empty()) plmError(H.planErr);

    static const char* kSev[] = {"low", "med", "high", "critical"};

    // ---- top band: Issues + Milestones as two discrete glass panels ------------
    const float availTop = ImGui::GetContentRegionAvail().x;
    const float halfW = (availTop - 12 * S) * 0.5f;

    // Issues — log a problem with a severity, so you can plan fixes while you see
    // what's wrong. "Make plan" (on each row) turns one into a linked task.
    plmPanelBegin("##pl_issues", "Issues", "issue_bug", halfW);
    ImGui::SetNextItemWidth(90 * S);
    ImGui::Combo("##newissuesev", &H.newIssueSev, kSev, IM_ARRAYSIZE(kSev));
    ImGui::SameLine();
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

    // (Team & Sync moved to Settings → gives the boards below more vertical room.)
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
    ImGui::SetNextItemWidth(86 * S);
    ImGui::Combo("##ntpri", &s_newTaskPri, kPriWord, IM_ARRAYSIZE(kPriWord));
    ImGui::SameLine();
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
            plmCardBegin("##task", sel, 8.0f, 3.0f, plmTintPriority(t.priority), 0.22f);

            // Title wraps to the card width so the card auto-grows (AutoResizeY) to
            // fit a long title instead of clipping it to one line.
            ImGui::PushTextWrapPos(0.0f);
            ImGui::PushStyleColor(ImGuiCol_Text,
                ImVec4(palette.text[0], palette.text[1], palette.text[2], 1.0f));
            ImGui::TextUnformatted(t.title.c_str());
            ImGui::PopStyleColor();
            ImGui::PopTextWrapPos();
            if (!t.assignee.empty()) dimText("@%s", t.assignee.c_str());

            // Movement controls — always visible, one click, no select-first step.
            // ‹ › walk the todo→doing→done lane; Block parks the task. The row is
            // dropped down a touch and shares a baseline with the priority dot.
            int idx = -1;
            for (int k = 0; k < 3; ++k) if (t.status == kPipe[k]) idx = k;
            bool blocked = (idx < 0);
            ImGui::Dummy(ImVec2(0, 2 * S));   // nudge the control row + dot down
            ImGui::PushStyleVar(ImGuiStyleVar_FramePadding, ImVec2(4 * S, 1 * S));
            ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing, ImVec2(3 * S, 3 * S));
            // ArrowButton draws a real triangle (font-independent — the ‹ › glyphs
            // were not in the atlas and rendered blank); the tight FramePadding
            // above shrinks its square to a compact size.
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
            if (sel) {
                ImGui::SameLine();
                ImGui::PushStyleColor(ImGuiCol_Button,
                    ImVec4(palette.statusErr[0], palette.statusErr[1], palette.statusErr[2], 0.55f));
                if (ImGui::SmallButton("Del"))
                    planCall(st, "delete", "task", nullptr, t.id);
                ImGui::PopStyleColor();
            }
            // Priority dot on the SAME line as the controls — drawn while the compact
            // FramePadding is still pushed so its height matches the buttons and they
            // share a vertical centre. Right-pinned inside the card.
            priorityDot(t.priority);
            ImGui::PopStyleVar(2);
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

void severityBadge(const std::string& sev) {
    const float* c = palette.textDim;
    if (sev == "critical" || sev == "high") c = palette.statusErr;
    else if (sev == "med") c = palette.statusWarn;
    ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(c[0], c[1], c[2], 1.0f));
    ImGui::Text("[%s]", sev.empty() ? "med" : sev.c_str());
    ImGui::PopStyleColor();
}
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

}  // namespace ui
