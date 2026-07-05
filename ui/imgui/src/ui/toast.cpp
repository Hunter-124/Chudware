#include "ui/toast.h"
#include "app/app_state.h"
#include "theme/theme.h"
#include "imgui.h"
#include <cfloat>

namespace ui {

namespace {

static std::mutex              s_toastMtx;
static std::vector<Toast>      s_toasts;
static int                     s_firedAction = 0;  // T-R5: clicked toast action

void levelColors(ToastLevel lvl, unsigned int& accentRole, const char*& tag) {
    const theme::Palette& p = theme::pal();
    switch (lvl) {
    case ToastLevel::Error: accentRole = p.bad;  tag = "Error";   break;
    case ToastLevel::Warn:  accentRole = p.warn; tag = "Warning"; break;
    default:                accentRole = p.good; tag = "Info";    break;
    }
}

} // namespace

void postToast(ToastLevel level, std::string msg) {
    Toast t;
    t.level = level;
    t.msg   = std::move(msg);
    t.ttl   = toastTtl(level);
    t.born  = ImGui::GetTime();
    std::lock_guard<std::mutex> lk(s_toastMtx);
    s_toasts.push_back(std::move(t));
}

void postToastAction(ToastLevel level, std::string msg,
                     std::string actionLabel, int actionId, double ttl) {
    Toast t;
    t.level       = level;
    t.msg         = std::move(msg);
    t.ttl         = ttl;
    t.born        = ImGui::GetTime();
    t.actionLabel = std::move(actionLabel);
    t.actionId    = actionId;
    std::lock_guard<std::mutex> lk(s_toastMtx);
    s_toasts.push_back(std::move(t));
}

int takeToastAction() {
    std::lock_guard<std::mutex> lk(s_toastMtx);
    int id = s_firedAction;
    s_firedAction = 0;
    return id;
}

void drawToasts(app::AppState& st) {
    (void)st;
    std::lock_guard<std::mutex> lk(s_toastMtx);
    if (s_toasts.empty()) return;

    const double now = ImGui::GetTime();
    for (auto& t : s_toasts)
        if (t.born <= 0.0) t.born = now;
    for (size_t i = 0; i < s_toasts.size();) {
        if (now - s_toasts[i].born >= s_toasts[i].ttl)
            s_toasts.erase(s_toasts.begin() + (ptrdiff_t)i);
        else
            ++i;
    }
    if (s_toasts.empty()) return;

    const float S = theme::S();
    const theme::Palette& p = theme::pal();

    ImGuiViewport* vp = ImGui::GetMainViewport();
    const float pad      = theme::U(1.5f);
    const float toastW   = 320.f * S;
    const float rowGap   = theme::U(1.f);
    const float topInset = 64.f * S;
    float x = vp->Pos.x + pad;
    float y = vp->Pos.y + topInset;

    ImDrawList* dl   = ImGui::GetForegroundDrawList();
    ImFont* font     = theme::font(theme::Font::Label);
    if (!font) font = ImGui::GetFont();

    const ImVec2 mouse  = ImGui::GetMousePos();
    const bool clicked  = ImGui::IsMouseClicked(ImGuiMouseButton_Left);
    int dismissIdx      = -1;

    int shown = 0;
    for (int i = (int)s_toasts.size() - 1; i >= 0 && shown < 5; --i, ++shown) {
        const Toast& t = s_toasts[(size_t)i];
        unsigned int accentRole = p.good;
        const char* tag = "Info";
        levelColors(t.level, accentRole, tag);

        const float innerW = toastW - theme::U(2.5f);
        ImVec2 msgSize = font->CalcTextSizeA(font->FontSize, FLT_MAX, innerW, t.msg.c_str());
        float titleH = font->FontSize + theme::U(0.25f);
        // Action toasts get an extra clickable row under the message (T-R5).
        const bool hasAction = !t.actionLabel.empty();
        const float actionH = hasAction ? (font->FontSize + theme::U(0.75f)) : 0.f;
        float cardH  = titleH + msgSize.y + actionH + theme::U(2.f);

        ImVec2 p0(x, y);
        ImVec2 p1(x + toastW, y + cardH);

        float life  = (float)(t.ttl - (now - t.born));
        float alpha = life < 0.6f ? (life / 0.6f) : 1.f;
        alpha = alpha < 0.f ? 0.f : (alpha > 1.f ? 1.f : alpha);

        auto withA = [&](unsigned int role, float mul) {
            return theme::roleU32(role, mul * alpha);
        };

        const std::string slotId = "toast_glass_" + std::to_string(shown);
        const theme::AdaptiveSurface& surf =
            theme::adaptivePanel(theme::panelId(slotId.c_str()), ImRect(p0, p1));
        theme::glassPanel(dl, ImRect(p0, p1), theme::MaterialLevel::Regular,
                          theme::ctx().m.roundingLg, surf, alpha);
        dl->AddRectFilled(p0, ImVec2(p0.x + 3.f * S, p1.y), withA(accentRole, 1.f),
                          theme::ctx().m.rounding * S, ImDrawFlags_RoundCornersLeft);

        float tx = p0.x + theme::U(1.5f);
        float ty = p0.y + theme::U(1.f);
        dl->AddText(font, font->FontSize, ImVec2(tx, ty), withA(accentRole, 1.f), tag);
        ImVec4 msgCol = surf.textPrimary;
        msgCol.w *= alpha;
        dl->AddText(font, font->FontSize, ImVec2(tx, ty + titleH),
                    ImGui::ColorConvertFloat4ToU32(msgCol), t.msg.c_str(), nullptr, innerW);
        bool actionHit = false;
        if (hasAction) {
            // Accent-colored, underlined action label — the whole label is the hit area.
            const float ay = ty + titleH + msgSize.y + theme::U(0.5f);
            const ImVec2 asz = font->CalcTextSizeA(font->FontSize, FLT_MAX, innerW,
                                                   t.actionLabel.c_str());
            const bool hov = mouse.x >= tx && mouse.x <= tx + asz.x &&
                             mouse.y >= ay && mouse.y <= ay + asz.y;
            const ImU32 acol = withA(p.accent, hov ? 1.f : 0.85f);
            dl->AddText(font, font->FontSize, ImVec2(tx, ay), acol,
                        t.actionLabel.c_str(), nullptr, innerW);
            dl->AddLine(ImVec2(tx, ay + asz.y), ImVec2(tx + asz.x, ay + asz.y),
                        acol, 1.f * S);
            if (clicked && hov) {
                s_firedAction = t.actionId;
                dismissIdx = i;
                actionHit = true;
            }
        }
        if (!actionHit && clicked && mouse.x >= p0.x && mouse.x <= p1.x &&
            mouse.y >= p0.y && mouse.y <= p1.y) {
            ImGui::SetClipboardText(t.msg.c_str());
            dismissIdx = i;
        }

        y += cardH + rowGap;
    }

    if (dismissIdx >= 0)
        s_toasts.erase(s_toasts.begin() + dismissIdx);
}

} // namespace ui
