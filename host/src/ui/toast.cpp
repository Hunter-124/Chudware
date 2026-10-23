// Toast notification renderer (W1-B). See toast.h.
// Restyled from the glass-card + left-accent-bar look to the app-wide
// active-indicator rule — opaque popupBg card with a BOLD severity-colored OUTLINE around the
// whole bounding box (error=statusErr, warn=statusWarn, success=statusOk, info=neutral textDim),
// a small drawn severity icon, single-click-anywhere dismiss (click swallowed so it doesn't fall
// through to the viewport), identical-consecutive dedup with an xN badge, and a hard visible cap
// with a "+N more" summary row. All colors are ui::palette roles — valid in both themes.
#include "toast.h"

#include <cfloat>
#include <cstdio>
#include <cstdlib>
#include <string>

#include "imgui.h"

#include "../core/app_state.h"
#include "../ui_theme.h"
#include "layout.h"  // ui::overlayBand — anchor below the REAL ribbon bottom, not a guessed inset

namespace ui {

// Map a toast level to its theme-role accent + title tag. Info is deliberately neutral (textDim),
// not the teal accent — only genuine status levels get a colored outline.
static void levelStyle(ToastLevel lvl, const float*& accent, const char*& tag) {
    const auto& pal = ui::palette;
    switch (lvl) {
        case ToastLevel::Error:   accent = pal.statusErr;  tag = "Error";   break;
        case ToastLevel::Warn:    accent = pal.statusWarn; tag = "Warning"; break;
        case ToastLevel::Success: accent = pal.statusOk;   tag = "Success"; break;
        default:                  accent = pal.textDim;    tag = "Info";    break;
    }
}

// Draw the severity glyph inside a circle at center c with radius r (all sizes pre-scaled).
static void drawIcon(ImDrawList* dl, ToastLevel lvl, ImVec2 c, float r, ImU32 col, float th) {
    dl->AddCircle(c, r, col, 0, th);
    switch (lvl) {
        case ToastLevel::Success: {  // check mark
            ImVec2 a(c.x - r * 0.45f, c.y + r * 0.05f);
            ImVec2 b(c.x - r * 0.12f, c.y + r * 0.40f);
            ImVec2 d(c.x + r * 0.50f, c.y - r * 0.35f);
            dl->AddLine(a, b, col, th);
            dl->AddLine(b, d, col, th);
            break;
        }
        case ToastLevel::Error: {  // X
            float k = r * 0.42f;
            dl->AddLine(ImVec2(c.x - k, c.y - k), ImVec2(c.x + k, c.y + k), col, th);
            dl->AddLine(ImVec2(c.x - k, c.y + k), ImVec2(c.x + k, c.y - k), col, th);
            break;
        }
        case ToastLevel::Warn: {  // exclamation
            dl->AddLine(ImVec2(c.x, c.y - r * 0.50f), ImVec2(c.x, c.y + r * 0.12f), col, th);
            dl->AddCircleFilled(ImVec2(c.x, c.y + r * 0.48f), th * 0.75f, col);
            break;
        }
        default: {  // "i"
            dl->AddCircleFilled(ImVec2(c.x, c.y - r * 0.48f), th * 0.75f, col);
            dl->AddLine(ImVec2(c.x, c.y - r * 0.12f), ImVec2(c.x, c.y + r * 0.50f), col, th);
            break;
        }
    }
}

void toastWithAction(AppState& st, ToastLevel level, std::string msg,
                     std::string actionLabel, std::function<void(AppState&)> action) {
    Toast t;
    t.level = level;
    t.msg = std::move(msg);
    t.born = 0.0;
    t.ttl = toastTtl(level) + 6.0;   // longer life so the action stays reachable
    t.actionLabel = std::move(actionLabel);
    t.action = std::move(action);
    std::lock_guard<std::mutex> lk(st.toastsMtx);
    st.toasts.push_back(std::move(t));
    if (st.toasts.size() > 32)
        st.toasts.erase(st.toasts.begin(), st.toasts.begin() + (st.toasts.size() - 32));
}

void drawToasts(AppState& st) {
    // C39: the queue is also written from background threads (worker errorSink); hold the
    // lock for the whole pass — contention is a rare sub-microsecond push_back.
    std::lock_guard<std::mutex> lk(st.toastsMtx);

    // Visual-QA hook: CHUDWARE_TOAST_DEMO=1 seeds one representative batch (dup storm for the xN
    // badge, every severity, overflow rows for the "+N more" cap) with long ttls so a headless
    // --ui-screenshot can capture the styling. No effect unless the env var is set.
    static bool demoSeeded = false;
    if (!demoSeeded && std::getenv("CHUDWARE_TOAST_DEMO")) {
        demoSeeded = true;
        auto push = [&](ToastLevel l, const char* m) {
            Toast t; t.level = l; t.msg = m; t.ttl = toastTtl(l) + 120.0;
            st.toasts.push_back(std::move(t));
        };
        for (int k = 0; k < 5; ++k)
            push(ToastLevel::Error, "Open failed: live.step — unknown method: doc_open");
        push(ToastLevel::Info, "Autosave complete");
        push(ToastLevel::Info, "Overflow demo row A");
        push(ToastLevel::Info, "Overflow demo row B");
        push(ToastLevel::Warn, "Mesh quality below target in 2 regions");
        push(ToastLevel::Success, "Toolpath posted (grbl dialect)");
    }

    if (st.toasts.empty()) return;

    const double now = ImGui::GetTime();

    // Collapse identical consecutive toasts (same level + message) into the NEWER entry so a
    // burst of repeated errors ("Open failed" x5 at startup) shows one card with an xN badge.
    // Keeping the newer entry means the lifetime refreshes while the storm continues.
    for (size_t i = 1; i < st.toasts.size();) {
        // Toasts carrying an action never collapse — each keeps its own reversible affordance.
        if (st.toasts[i].level == st.toasts[i - 1].level &&
            st.toasts[i].msg == st.toasts[i - 1].msg &&
            !st.toasts[i].action && !st.toasts[i - 1].action) {
            st.toasts[i].count += st.toasts[i - 1].count;
            st.toasts.erase(st.toasts.begin() + (i - 1));
        } else {
            ++i;
        }
    }

    // Stamp newly-posted toasts and drop expired ones (oldest-first scan; order preserved).
    for (auto& t : st.toasts)
        if (t.born <= 0.0) t.born = now;
    for (size_t i = 0; i < st.toasts.size();) {
        if (now - st.toasts[i].born >= st.toasts[i].ttl)
            st.toasts.erase(st.toasts.begin() + i);
        else
            ++i;
    }
    if (st.toasts.empty()) return;

    const float scale = ui::uiScale;
    const auto& pal = ui::palette;

    // Anchor top-RIGHT of the viewport span the pinned columns leave clear, below the REAL ribbon
    // bottom (a fixed 64dp guess undershot the ribbon's actual height and pinned the
    // card over its leftmost groups). overlayBand().minY is the same ribbon-bottom Y the
    // FeatureManager/Copilot columns already anchor to; fall back to a modest fixed inset for the
    // pre-publish first frame.
    ImGuiViewport* vp = ImGui::GetMainViewport();
    const float pad     = 12.0f * scale;
    float toastW        = 320.0f * scale;
    const float rowGap  = 8.0f * scale;
    const float rounding = 6.0f * scale;
    const ui::OverlayBand& band = ui::overlayBand();
    // Start below the doc-tab band when it drew (the pinned columns anchor there too), so a toast
    // never covers the open-document tabs.
    float topY = band.valid ? band.minY : vp->Pos.y + 56.0f * scale;
    if (const float tabsBottom = ui::docTabBandBottom(); tabsBottom > topY) topY = tabsBottom;
    const float topInset = (topY - vp->Pos.y) + 8.0f * scale;
    // The stack's right edge is the Copilot column's published LEFT edge (layout.h slot 1) when
    // that column is pinned, never the window edge: anchoring to the window edge put every toast
    // over the Copilot header (a recurring regression). The column publishes its edge even while
    // rolled up to its header row, which still owns the top-right corner. The left bound is the
    // FeatureManager column's right edge, so a narrow window shrinks the card between the two
    // columns instead of sliding it under either.
    float rightX = vp->Pos.x + vp->Size.x;
    if (band.valid) rightX = band.maxX;
    if (const float cpEdge = ui::overlayColumnEdge(1); cpEdge > 0.0f && cpEdge < rightX)
        rightX = cpEdge;
    float leftX = vp->Pos.x;
    if (const float fmEdge = ui::overlayColumnEdge(0); fmEdge > leftX && fmEdge < rightX)
        leftX = fmEdge;
    const float spanW = rightX - leftX - 2.0f * pad;
    const float minToastW = 180.0f * scale;
    if (spanW < toastW) toastW = spanW > minToastW ? spanW : minToastW;
    float x = rightX - toastW - pad;
#if defined(__EMSCRIPTEN__)
    // The short mobile web layout paints a horizontal DOM HUD across the rightmost ~360 ImGui
    // pixels. Keep toasts in the left span instead; the HUD lane is already expressed in viewport
    // coordinates, so unlike the card's dp metrics it must not be multiplied by uiScale.
    if (vp->Size.x <= 900.0f && vp->Size.y <= 600.0f) {
        constexpr float hudLaneW = 360.0f;
        const float readableFloor = 120.0f * scale;
        const float leftSpanW = vp->Size.x - hudLaneW - 2.0f * pad;

        // Never let a minimum width push the card back into the HUD lane. The readable floor is
        // relevant only when it fits; below that, honoring the actual span is the safer bound.
        if (leftSpanW >= readableFloor)
            toastW = leftSpanW < toastW ? leftSpanW : toastW;
        else if (leftSpanW > 0.0f)
            toastW = leftSpanW;
        x = vp->Pos.x + pad;
    }
#endif
    float y = vp->Pos.y + topInset;

    ImDrawList* dl = ImGui::GetForegroundDrawList();
    ImFont* font = st.fonts.medium ? st.fonts.medium : ImGui::GetFont();

    // Newest on top: iterate from the back of the queue (most-recent first).
    int shown = 0;
    const ImVec2 mouse = ImGui::GetMousePos();
    const bool clicked = ImGui::IsMouseClicked(ImGuiMouseButton_Left);
    int dismissIdx = -1;
    bool anyHovered = false;

    const float iconR   = 7.0f * scale;                    // severity-icon circle radius
    const float iconTh  = 1.6f * scale;                    // icon stroke
    const float outlineTh = 2.0f * scale;                  // BOLD severity outline

    for (int i = (int)st.toasts.size() - 1; i >= 0 && shown < kToastMaxVisible; --i, ++shown) {
        const Toast& t = st.toasts[i];
        const float* accent = nullptr;
        const char* tag = nullptr;
        levelStyle(t.level, accent, tag);

        // Wrap the message to the inner width to size the card height. The icon indents the
        // title row only; the message spans the full inner width beneath it.
        const float innerPad = 12.0f * scale;
        const float innerW = toastW - 2.0f * innerPad;
        ImVec2 msgSize = font->CalcTextSizeA(font->FontSize, FLT_MAX, innerW, t.msg.c_str());
        float titleH = font->FontSize + 4.0f * scale;
        const bool hasAction = !t.actionLabel.empty() && (bool)t.action;
        const float actionRowH = hasAction ? (font->FontSize + 16.0f * scale) : 0.0f;
        float cardH = titleH + msgSize.y + 18.0f * scale + actionRowH;

        ImVec2 p0(x, y);
        ImVec2 p1(x + toastW, y + cardH);

        const bool hovered = mouse.x >= p0.x && mouse.x <= p1.x &&
                             mouse.y >= p0.y && mouse.y <= p1.y;
        anyHovered = anyHovered || hovered;

        // Fade out over the last 0.6s of life (a hovered card holds full alpha so it doesn't
        // vanish under the cursor mid-read).
        float life = (float)(t.ttl - (now - t.born));
        float alpha = life < 0.6f ? (life / 0.6f) : 1.0f;
        if (alpha < 0.0f) alpha = 0.0f;
        if (alpha > 1.0f) alpha = 1.0f;
        if (hovered && alpha > 0.0f) alpha = 1.0f;

        auto withA = [&](const float c[4], float mul) {
            float v[4] = {c[0], c[1], c[2], c[3] * mul * alpha};
            return ui::Palette::toU32(v);
        };

        // Opaque card + bold severity outline around the whole bounding box (app-wide active-
        // indicator rule: no accent bars/lines — outlines). Hover brightens the fill slightly.
        dl->AddRectFilled(p0, p1, withA(hovered ? pal.buttonHovered : pal.popupBg, 1.0f), rounding);
        dl->AddRect(p0, p1, withA(accent, hovered ? 1.0f : 0.9f), rounding, 0, outlineTh);

        // Title row: severity icon + level tag (+ xN badge when duplicates collapsed).
        float tx = p0.x + innerPad;
        float ty = p0.y + 9.0f * scale;
        ImVec2 iconC(tx + iconR, ty + titleH * 0.5f - 2.0f * scale);
        drawIcon(dl, t.level, iconC, iconR, withA(accent, 1.0f), iconTh);
        float tagX = tx + iconR * 2.0f + 6.0f * scale;
        dl->AddText(font, font->FontSize, ImVec2(tagX, ty), withA(accent, 1.0f), tag);

        if (t.count > 1) {
            char badge[16];
            snprintf(badge, sizeof(badge), "x%d", t.count);
            ImVec2 bSize = font->CalcTextSizeA(font->FontSize, FLT_MAX, 0.0f, badge);
            float bPadX = 6.0f * scale, bPadY = 2.0f * scale;
            ImVec2 b1(p1.x - innerPad, ty + font->FontSize + bPadY);
            ImVec2 b0(b1.x - bSize.x - 2.0f * bPadX, ty - bPadY);
            dl->AddRectFilled(b0, b1, withA(accent, 0.22f), (b1.y - b0.y) * 0.5f);
            dl->AddRect(b0, b1, withA(accent, 0.9f), (b1.y - b0.y) * 0.5f, 0, 1.0f * scale);
            dl->AddText(font, font->FontSize, ImVec2(b0.x + bPadX, ty), withA(accent, 1.0f), badge);
        }

        // Message body — primary text role on the opaque card (both themes).
        dl->AddText(font, font->FontSize, ImVec2(tx, ty + titleH), withA(pal.text, 1.0f),
                    t.msg.c_str(), nullptr, innerW);

        // Single click anywhere on the card = copy message to OS clipboard + instant dismiss.
        // An invisible ImGui window sized to the card claims mouse hover/clicks so the click
        // does NOT fall through to the viewport underneath (a raw io.MouseClicked check would
        // dismiss but still let the viewport pick — drawToasts runs after the panels, so
        // clearing the click flag here can't retro-swallow it). Hover routing for a window is
        // one frame delayed on first appearance; the manual hovered check above covers that
        // first frame as a dismiss fallback.
        {
            char winId[32];
            snprintf(winId, sizeof(winId), "##toast_hit_%d", shown);
            ImGui::SetNextWindowPos(p0);
            ImGui::SetNextWindowSize(ImVec2(toastW, cardH));
            ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(0, 0));
            ImGui::PushStyleVar(ImGuiStyleVar_WindowBorderSize, 0.0f);
            ImGui::PushStyleColor(ImGuiCol_WindowBg, ImVec4(0, 0, 0, 0));
            ImGui::Begin(winId, nullptr,
                         ImGuiWindowFlags_NoTitleBar | ImGuiWindowFlags_NoResize |
                             ImGuiWindowFlags_NoMove | ImGuiWindowFlags_NoScrollbar |
                             ImGuiWindowFlags_NoSavedSettings | ImGuiWindowFlags_NoNav |
                             ImGuiWindowFlags_NoFocusOnAppearing);
            // Dismiss hit-area covers the card EXCEPT the action row (so the action button
            // below can claim its own clicks).
            bool hit = ImGui::InvisibleButton("hit", ImVec2(toastW, cardH - actionRowH));
            bool firstFrameFallback = clicked && hovered && !ImGui::IsItemHovered();
            bool actionClicked = false;
            if (hasAction) {
                ImGui::SetCursorPos(ImVec2(innerPad, cardH - actionRowH + 3.0f * scale));
                ImGui::PushStyleColor(ImGuiCol_Button, withA(accent, 0.22f));
                ImGui::PushStyleColor(ImGuiCol_ButtonHovered, withA(accent, 0.40f));
                ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(pal.text));
                actionClicked = ImGui::Button(t.actionLabel.c_str());
                ImGui::PopStyleColor(3);
            }
            ImGui::End();
            ImGui::PopStyleColor();
            ImGui::PopStyleVar(2);
            if (actionClicked) {
                auto fn = t.action;   // copy before the queue mutates
                if (fn) fn(st);
                dismissIdx = i;
            } else if (hit || firstFrameFallback) {
                ImGui::SetClipboardText(t.msg.c_str());
                dismissIdx = i;
            }
        }

        y += cardH + rowGap;
    }

    // Overflow summary: queue beyond the visible cap waits behind a quiet "+N more" row.
    int hidden = (int)st.toasts.size() - shown;
    if (hidden > 0) {
        char more[32];
        snprintf(more, sizeof(more), "+%d more", hidden);
        ImVec2 mSize = font->CalcTextSizeA(font->FontSize, FLT_MAX, 0.0f, more);
        float mh = font->FontSize + 10.0f * scale;
        ImVec2 p0(x, y);
        ImVec2 p1(x + mSize.x + 24.0f * scale, y + mh);
        dl->AddRectFilled(p0, p1, ui::Palette::toU32(pal.popupBg), rounding);
        dl->AddRect(p0, p1, ui::Palette::toU32(pal.border), rounding, 0, 1.0f * scale);
        dl->AddText(font, font->FontSize,
                    ImVec2(p0.x + 12.0f * scale, p0.y + 5.0f * scale),
                    ui::Palette::toU32(pal.textDim), more);
    }

    if (anyHovered) ImGui::SetMouseCursor(ImGuiMouseCursor_Hand);

    if (dismissIdx >= 0) st.toasts.erase(st.toasts.begin() + dismissIdx);
}

}  // namespace ui
