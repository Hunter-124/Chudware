// Toast notification renderer (W1-B). See toast.h.
// Liquid Glass v2 port (host, H6b write-set): each toast card is a Regular glass surface fed by
// its own per-slot adaptivePanel (id keyed on the visible slot index so it stays stable while
// toasts scroll through the stack); message text reads surf.textPrimary for max contrast against
// the live viewport backdrop. Ported from ui/imgui/src/ui/toast.cpp's drawToasts. Accent bars stay
// raw palette (semantic level color); the title tag is the same role tone-matched to the frost
// (contract: local://glass_host.md + viewport-text contrast audit).
#include "toast.h"

#include <cfloat>
#include <string>

#include "imgui.h"
#include "imgui_internal.h"   // ImRect — ui::glassPanel/ui::adaptivePanel take a const ImRect&

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

    const float scale = ui::uiScale;

    // Anchor top-LEFT of the main viewport (toasts float over everything, SolidWorks-style).
    // A modest inset from the left edge and below the title/ribbon area.
    ImGuiViewport* vp = ImGui::GetMainViewport();
    const float pad     = 12.0f * scale;
    const float toastW  = 320.0f * scale;
    const float rowGap  = 8.0f * scale;
    const float topInset = 64.0f * scale;  // clear the title bar + ribbon
    float x = vp->Pos.x + pad;
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

        // Per-toast glass card: adaptivePanel samples the backdrop under THIS card's rect (cards
        // shift position frame to frame as older toasts expire, so the id must key on slot, not
        // identity); glassPanel handles the backdrop-UV projection + edge effects, dimmed by alpha.
        const std::string slotId = "host_toast_glass_" + std::to_string(shown);
        const ui::AdaptiveSurface& surf =
            ui::adaptivePanel(ui::panelId(slotId.c_str()), ImRect(p0, p1));
        ui::glassPanel(dl, ImRect(p0, p1), ui::GlassLevel::Regular, 8.0f /*logical dp — glassPanel scales*/, surf, alpha);
        // Colored left accent bar drawn on top of glass; withA already folds in alpha.
        dl->AddRectFilled(p0, ImVec2(p0.x + 3.0f * scale, p1.y), withA(accent, 1.0f),
                          6.0f * scale, ImDrawFlags_RoundCornersLeft);

        // Title (level tag) + message body. The tag ink is tone-matched to the card frost
        // (contrast audit P1): the raw statusOk/Warn roles are bright in the dark theme and wash
        // out on the light frost the viewport backdrop produces, so deepen the role toward black
        // when the surface wants dark text and lift it toward white over a dark frost. The left
        // accent bar keeps the raw semantic role.
        float tx = p0.x + 12.0f * scale;
        float ty = p0.y + 8.0f * scale;
        float tagC[4];
        if (surf.darkText)
            for (int ch = 0; ch < 3; ++ch) tagC[ch] = accent[ch] * 0.55f;
        else
            for (int ch = 0; ch < 3; ++ch) tagC[ch] = accent[ch] + (1.0f - accent[ch]) * 0.45f;
        tagC[3] = 1.0f;
        dl->AddText(font, font->FontSize, ImVec2(tx, ty), withA(tagC, 1.0f), tag);
        ImVec4 msgCol = surf.textPrimary;
        msgCol.w *= alpha;
        dl->AddText(font, font->FontSize, ImVec2(tx, ty + titleH),
                    ImGui::ColorConvertFloat4ToU32(msgCol), t.msg.c_str(), nullptr, innerW);

        // Click = copy message to OS clipboard + instant dismiss (no fade-out).
        if (clicked && mouse.x >= p0.x && mouse.x <= p1.x && mouse.y >= p0.y && mouse.y <= p1.y) {
            ImGui::SetClipboardText(t.msg.c_str());
            dismissIdx = i;
        }

        y += cardH + rowGap;
    }

    if (dismissIdx >= 0) st.toasts.erase(st.toasts.begin() + dismissIdx);
}

}  // namespace ui
