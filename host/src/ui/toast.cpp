// Toast notification renderer (W1-B). See toast.h.
#include "toast.h"

#include <cfloat>
#include <string>

#include "imgui.h"

#include "../core/app_state.h"
#include "../ui_theme.h"

namespace ui {

// Map a toast level to its theme-role accent (left bar + title tint) and a soft panel bg.
static void levelColors(ToastLevel lvl, const float*& accent, const char*& tag) {
    const auto& pal = ui::palette;
    switch (lvl) {
        case ToastLevel::Error: accent = pal.statusErr; tag = "Error"; break;
        case ToastLevel::Warn:  accent = pal.statusWarn; tag = "Warning"; break;
        default:                accent = pal.statusOk;  tag = "Info"; break;
    }
}

void drawToasts(AppState& st) {
    // C39: the queue is also written from background threads (worker errorSink); hold the
    // lock for the whole pass — contention is a rare sub-microsecond push_back.
    std::lock_guard<std::mutex> lk(st.toastsMtx);
    if (st.toasts.empty()) return;

    const double now = ImGui::GetTime();

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

    const auto& pal = ui::palette;
    const float scale = ui::uiScale;

    // Anchor top-right of the main viewport (the whole window — toasts float over everything,
    // SolidWorks-style). A modest inset from the right edge and below the title/ribbon area.
    ImGuiViewport* vp = ImGui::GetMainViewport();
    const float pad     = 12.0f * scale;
    const float toastW  = 320.0f * scale;
    const float rowGap  = 8.0f * scale;
    const float topInset = 64.0f * scale;  // clear the title bar + ribbon
    float x = vp->Pos.x + vp->Size.x - toastW - pad;
    float y = vp->Pos.y + topInset;

    ImDrawList* dl = ImGui::GetForegroundDrawList();
    ImFont* font = st.fonts.medium ? st.fonts.medium : ImGui::GetFont();

    // Newest on top: iterate from the back of the queue (most-recent first), max 5 visible.
    int shown = 0;
    const ImVec2 mouse = ImGui::GetMousePos();
    const bool clicked = ImGui::IsMouseClicked(ImGuiMouseButton_Left);
    int dismissIdx = -1;

    for (int i = (int)st.toasts.size() - 1; i >= 0 && shown < 5; --i, ++shown) {
        const Toast& t = st.toasts[i];
        const float* accent = nullptr;
        const char* tag = nullptr;
        levelColors(t.level, accent, tag);

        // Wrap the message to the inner width to size the card height.
        const float innerW = toastW - 20.0f * scale;
        ImVec2 msgSize = font->CalcTextSizeA(font->FontSize, FLT_MAX, innerW, t.msg.c_str());
        float titleH = font->FontSize + 2.0f * scale;
        float cardH = titleH + msgSize.y + 16.0f * scale;

        ImVec2 p0(x, y);
        ImVec2 p1(x + toastW, y + cardH);

        // Fade out over the last 0.6s of life.
        float life = (float)(t.ttl - (now - t.born));
        float alpha = life < 0.6f ? (life / 0.6f) : 1.0f;
        if (alpha < 0.0f) alpha = 0.0f;
        if (alpha > 1.0f) alpha = 1.0f;

        auto withA = [&](const float c[4], float mul) {
            float v[4] = {c[0], c[1], c[2], c[3] * mul * alpha};
            return ui::Palette::toU32(v);
        };

        // Panel bg (popup role), border, and a left accent bar in the level color.
        dl->AddRectFilled(p0, p1, withA(pal.popupBg, 0.98f), 6.0f * scale);
        dl->AddRect(p0, p1, withA(pal.border, 1.0f), 6.0f * scale, 0, 1.2f * scale);
        dl->AddRectFilled(p0, ImVec2(p0.x + 3.0f * scale, p1.y), withA(accent, 1.0f),
                          6.0f * scale, ImDrawFlags_RoundCornersLeft);

        // Title (level tag) + message body.
        float tx = p0.x + 12.0f * scale;
        float ty = p0.y + 8.0f * scale;
        dl->AddText(font, font->FontSize, ImVec2(tx, ty), withA(accent, 1.0f), tag);
        dl->AddText(font, font->FontSize, ImVec2(tx, ty + titleH),
                    withA(pal.text, 1.0f), t.msg.c_str(), nullptr, innerW);

        // Click-to-dismiss anywhere on the card.
        if (clicked && mouse.x >= p0.x && mouse.x <= p1.x && mouse.y >= p0.y && mouse.y <= p1.y)
            dismissIdx = i;

        y += cardH + rowGap;
    }

    if (dismissIdx >= 0) st.toasts.erase(st.toasts.begin() + dismissIdx);
}

}  // namespace ui
