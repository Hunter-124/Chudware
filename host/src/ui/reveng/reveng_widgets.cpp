// reveng_widgets.cpp — the RE-40 shared visual kit for the Reverse Engineering workspace.
// See reveng_widgets.h for the contract each widget owes its callers.
//
// House rules this TU obeys (and exists to make cheap for the RE panels):
//   • Every colour comes from levelColor()/levelColorVec() or a ui::palette role. No literals.
//   • Every dimension comes from ui::metrics::* (the re* tokens where one exists). No literals.
//   • Cards reuse the shared liquid-glass card look (ui::glassCardWash on a deferred draw
//     channel) — the exact idiom plmCardBegin/plmCardEnd use in ui/plm/plm_pages.cpp — so an
//     RE card and a PLM card read as the same product.
//   • Icons are only ever drawn from a caller-supplied name, and only when the atlas actually
//     has that cell. The kit never invents an icon name (that logs "[icons] unknown icon").
//
// GL-thread only, like the rest of ui/reveng.
#include "reveng_widgets.h"

#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <string>
#include <vector>

#include "imgui.h"
#include "imgui_internal.h"

#include "ui_theme.h"
#include "../icons.h"
#include "../metrics.h"

namespace ui {
namespace reveng {
namespace kit {
namespace {

// ---- dp vs. scaled-pixel radii ------------------------------------------------------------------
// ui::glassBackground / glassCardWash / glassSelectionGlow take a LOGICAL dp radius (they multiply
// by ui::uiScale themselves) while ui::metrics::re*Round() already returns scaled pixels. Convert
// back exactly once so the corner never double-scales at HiDPI. ImDrawList::Add*() rounding, by
// contrast, is raw pixels — pass the metrics token straight through there.
inline float dpOf(float scaledPx) {
    const float s = ui::metrics::scale();
    return s > 0.0f ? scaledPx / s : scaledPx;
}
inline float cardRoundDp() { return dpOf(ui::metrics::reCardRound()); }
inline float chipRoundDp() { return dpOf(ui::metrics::reChipRound()); }

// ---- text fitting -------------------------------------------------------------------------------
// Shorten `text` so it fits `maxW`, appending a horizontal ellipsis. Returns the ORIGINAL string
// when it already fits, so the common case allocates nothing.
//
// Why this exists: centring a too-long label and clipping the segment eats BOTH ends, which turned
// the stepper's "Reconstruct" into "econstru". Clipping is not ellipsizing.
//
// U+2026 is inside the atlas' 0x2010-0x2027 range (ui_theme.cpp uiGlyphRanges), so it renders.
// Arrows and box-drawing are NOT in the atlas — never put them in a rendered string.
inline std::string ellipsize(const char* text, float maxW) {
    if (!text || !*text) return std::string();
    if (maxW <= 0.0f || ImGui::CalcTextSize(text).x <= maxW) return std::string(text);

    static const char* kEll = "\xe2\x80\xa6";  // U+2026 HORIZONTAL ELLIPSIS
    const float ellW = ImGui::CalcTextSize(kEll).x;
    if (ellW >= maxW) return std::string();

    const std::string s(text);
    std::size_t n = s.size();
    while (n > 0) {
        // Never split a UTF-8 continuation byte.
        while (n > 0 && (static_cast<unsigned char>(s[n - 1]) & 0xC0) == 0x80) --n;
        if (n == 0) break;
        --n;
        while (n > 0 && (static_cast<unsigned char>(s[n]) & 0xC0) == 0x80) --n;
        const std::string cut = s.substr(0, n);
        if (ImGui::CalcTextSize(cut.c_str()).x + ellW <= maxW) return cut + kEll;
    }
    return std::string(kEll);
}

// ---- opacity / ratio constants ------------------------------------------------------------------
// These scale a palette role's alpha or are pure ratios; the COLOUR itself is always a palette role.
constexpr float kCardWashA     = 0.060f;  // card liquid-glass tint at the top edge
constexpr float kChipFillA     = 0.16f;
constexpr float kChipBorderA   = 0.55f;
constexpr float kRuleA         = 0.55f;
constexpr float kTrackA        = 0.85f;   // progress / segmented-control track
constexpr float kMatteA        = 0.85f;   // plate behind a letterboxed image
constexpr float kPlaceholderA  = 0.35f;
constexpr float kCaptionA      = 0.80f;
constexpr float kHoverRingA    = 0.55f;
constexpr float kTileBaseA     = 0.45f;
constexpr float kTileHoverA    = 0.70f;
constexpr float kNodeIdleA     = 0.45f;
constexpr float kNodeHaloA     = 0.28f;
constexpr float kPrimaryFillA  = 0.90f;
constexpr float kDangerFillA   = 0.85f;
constexpr float kDangerActA    = 0.70f;
constexpr float kSegSelA       = 0.85f;
constexpr float kSegHoverA     = 0.16f;
constexpr float kArrowDimA     = 0.80f;
constexpr float kStripA        = 0.72f;

constexpr float kTooltipWrapEm  = 26.0f;      // tooltip wrap width, in em
constexpr float kHeroAspect     = 9.0f / 16.0f;
constexpr float kEmptyIconScale = 2.0f;       // empty/blocked glyph = 2x the header icon tier
constexpr float kMarqueeFrac    = 0.28f;      // indeterminate bar segment, fraction of the track
constexpr float kMarqueeSpeed   = 0.55f;      // marquee cycles per second
constexpr float kSpinnerSpeed   = 4.2f;       // radians per second
constexpr float kSpinnerSweep   = 1.35f;      // arc length, multiples of PI
constexpr int   kSpinnerSegs    = 16;
constexpr float kPulseBase      = 1.55f;      // active step-node halo radius, x node radius
constexpr float kPulseAmp       = 0.18f;
constexpr float kPulseSpeed     = 3.0f;
constexpr int   kMaxDecimals    = 6;

// ---- palette plumbing ---------------------------------------------------------------------------
inline ImU32 roleU32(const float c[4], float alphaMul = 1.0f) {
    float t[4];
    Palette::withAlpha(c, t, alphaMul);
    return (ImU32)Palette::toU32(t);
}
inline ImVec4 roleVec(const float c[4], float alphaMul = 1.0f) {
    return ImVec4(c[0], c[1], c[2], c[3] * alphaMul);
}
inline const float* levelRole(Level l) {
    switch (l) {
        case Level::Ok:    return palette.statusOk;
        case Level::Warn:  return palette.statusWarn;
        case Level::Error: return palette.statusErr;
        case Level::Busy:  return palette.accent;
        case Level::Neutral:
        default:           return palette.textDim;
    }
}

// ---- small painters -----------------------------------------------------------------------------
void drawSpinner(ImDrawList* dl, ImVec2 c, float r, float thickness, ImU32 col) {
    const float a0 = (float)ImGui::GetTime() * kSpinnerSpeed;
    dl->PathClear();
    dl->PathArcTo(c, r, a0, a0 + IM_PI * kSpinnerSweep, kSpinnerSegs);
    dl->PathStroke(col, ImDrawFlags_None, thickness);
}

// The status dot every chip / inline row shares. Busy spins instead of sitting still, so a
// pending job can never look like a finished one.
void drawLevelDot(ImDrawList* dl, ImVec2 c, float r, Level l) {
    const ImU32 col = levelColor(l);
    if (l == Level::Busy) {
        drawSpinner(dl, c, r, ui::metrics::stroke(1.5f), col);
        return;
    }
    dl->AddCircleFilled(c, r, col);
}

// Draw a caller-named atlas icon centred at `center`. Unknown names draw NOTHING (rather than the
// magenta placeholder tile) — the kit never substitutes a guessed name.
void drawIconAt(ImDrawList* dl, const char* name, ImVec2 center, float sz, ImU32 col) {
    if (!name || !name[0] || sz <= 0.0f) return;
    if (ui::hasIconSdf(name)) {
        ui::drawIconGlass(dl, name, center, sz, col, 0);
        return;
    }
    const int cell = (sz >= ui::metrics::px(24.0f)) ? 32 : 20;
    const ui::IconUV ic = ui::getIcon(name, cell);
    if (!ic.valid) return;
    const float r = sz * 0.5f;
    dl->AddImage(ic.texId, ImVec2(center.x - r, center.y - r), ImVec2(center.x + r, center.y + r),
                 ic.uv0, ic.uv1, col);
}

// Hand-drawn warning triangle for BlockedState — hand-drawn precisely so the kit does not have to
// hardcode (and possibly miss) an atlas icon name.
void drawWarnGlyph(ImDrawList* dl, ImVec2 c, float sz, ImU32 col) {
    const float r = sz * 0.5f;
    const float t = ui::metrics::stroke(1.6f);
    const ImVec2 apex(c.x, c.y - r);
    const ImVec2 bl(c.x - r, c.y + r * 0.72f);
    const ImVec2 br(c.x + r, c.y + r * 0.72f);
    dl->AddTriangle(apex, bl, br, col, t);
    dl->AddLine(ImVec2(c.x, c.y - r * 0.20f), ImVec2(c.x, c.y + r * 0.22f), col, t);
    dl->AddCircleFilled(ImVec2(c.x, c.y + r * 0.50f), ui::metrics::stroke(1.4f), col);
}

// Wrapped hover tooltip. No-op on an empty string so callers can pass an optional reason straight
// through. ImGuiCol_PopupBg is already the glass role (applyTheme), so no extra glass push here.
void kitTooltip(const char* text) {
    if (!text || !text[0]) return;
    if (!ImGui::BeginTooltip()) return;
    ImGui::PushTextWrapPos(ImGui::GetFontSize() * kTooltipWrapEm);
    ImGui::TextUnformatted(text);
    ImGui::PopTextWrapPos();
    ImGui::EndTooltip();
}

// Aspect-preserving centred image inside [a,b], on a matte plate so a transparent PNG still reads
// as a framed plate rather than a hole. texW/texH <= 0 => fill the box (already-square thumbnails).
void drawLetterboxed(ImDrawList* dl, ImTextureID tex, int texW, int texH, ImVec2 a, ImVec2 b) {
    const float boxW = b.x - a.x;
    const float boxH = b.y - a.y;
    if (boxW <= 0.0f || boxH <= 0.0f) return;
    dl->AddRectFilled(a, b, roleU32(palette.codeBg, kMatteA), ui::metrics::reChipRound());

    float aspect = (texW > 0 && texH > 0) ? ((float)texW / (float)texH) : (boxW / boxH);
    if (!(aspect > 0.0f)) aspect = 1.0f;
    float w = boxW;
    float h = boxW / aspect;
    if (h > boxH) { h = boxH; w = boxH * aspect; }
    const ImVec2 m((a.x + b.x) * 0.5f, (a.y + b.y) * 0.5f);
    dl->AddImage(tex, ImVec2(m.x - w * 0.5f, m.y - h * 0.5f), ImVec2(m.x + w * 0.5f, m.y + h * 0.5f));
}

// The "no texture" plate: a bounded, labelled box. Never a blank hole.
void drawPlaceholder(ImDrawList* dl, ImVec2 a, ImVec2 b, const char* label) {
    if (b.x <= a.x || b.y <= a.y) return;
    const float rnd = ui::metrics::reChipRound();
    const float thin = ui::metrics::stroke(1.0f);
    dl->AddRectFilled(a, b, roleU32(palette.codeBg, kPlaceholderA), rnd);
    dl->AddRect(a, b, roleU32(palette.border, kChipBorderA), rnd, 0, thin);
    dl->AddLine(a, b, roleU32(palette.border, kPlaceholderA), thin);
    dl->AddLine(ImVec2(a.x, b.y), ImVec2(b.x, a.y), roleU32(palette.border, kPlaceholderA), thin);
    if (!label || !label[0]) return;

    const ImVec2 ts = ImGui::CalcTextSize(label);
    const float padX = ui::metrics::U(1.0f);
    const float padY = ui::metrics::U(0.5f);
    if (ts.x + padX * 2.0f > (b.x - a.x) || ts.y + padY * 2.0f > (b.y - a.y)) return;
    const ImVec2 m((a.x + b.x) * 0.5f, (a.y + b.y) * 0.5f);
    dl->AddRectFilled(ImVec2(m.x - ts.x * 0.5f - padX, m.y - ts.y * 0.5f - padY),
                      ImVec2(m.x + ts.x * 0.5f + padX, m.y + ts.y * 0.5f + padY),
                      roleU32(palette.glassBg, kCaptionA), rnd);
    dl->AddText(ImVec2(m.x - ts.x * 0.5f, m.y - ts.y * 0.5f), roleU32(palette.textDim), label);
}

// ---- chip geometry (shared by StatusChip / ValueChip / HonestyTag / badges) ----------------------
struct ChipGeom {
    float  w = 0.0f, h = 0.0f;
    float  padX = 0.0f, padY = 0.0f, gap = 0.0f, dotR = 0.0f;
    ImVec2 textSize = ImVec2(0.0f, 0.0f);
};

ChipGeom chipGeom(const char* label, bool withDot) {
    ChipGeom g;
    g.padX = ui::metrics::U(1.5f);
    g.padY = ui::metrics::U(0.5f);
    g.gap  = ui::metrics::reLabelGap();
    g.dotR = withDot ? ui::metrics::U(0.75f) : 0.0f;
    g.textSize = ImGui::CalcTextSize(label ? label : "");
    g.h = g.textSize.y + g.padY * 2.0f;
    g.w = g.padX * 2.0f + g.textSize.x + (withDot ? (g.dotR * 2.0f + g.gap) : 0.0f);
    return g;
}

// ---- card nesting -------------------------------------------------------------------------------
// One entry per live CardBegin(). CardEnd() is a hard no-op on an empty stack, so an unmatched End
// (or an End after a CardBegin that returned false) can never pop ImGui state it did not push.
struct CardFrame {
    int   styleVars    = 0;
    int   styleColors  = 0;
    float contentLeft  = 0.0f;   // screen x of the card's content column
    float contentRight = 0.0f;
    bool  hasHeader    = false;
    bool  open         = true;
    // Screen x just past the header's chevron/icon/title. CardHeaderRight() right-aligns against
    // contentRight but must never rewind PAST this, or a wide status chip lands on top of the
    // title (which is exactly what "Dimensions" + a "fit-critical" chip did).
    float headerEndX   = 0.0f;
};
std::vector<CardFrame> g_cards;

// ---- 3D preview slots ---------------------------------------------------------------------------
constexpr int kSlotCount = 6;   // None, SparseCloud, Splat, Mesh, Deviation, Parametric
struct SlotRec {
    ImRect rect;
    bool   drawn = false;
};
SlotRec g_slots[kSlotCount];

inline int slotIndex(PreviewSlot s) {
    const int i = (int)s;
    return (i >= 0 && i < kSlotCount) ? i : 0;
}

// ---- buttons ------------------------------------------------------------------------------------
// Shared body for Primary/Secondary/Danger. A disabled button still renders (greyed) and still
// answers the user's "why can't I click this?" via `disabledReason` on hover — the kit never hides
// an action the user is looking for.
bool kitButton(const char* label, bool enabled, const char* disabledReason, const ImVec2& size,
               const ImVec4& base, const ImVec4& hovered, const ImVec4& active,
               const ImVec4& text) {
    ImVec2 sz = size;
    if (sz.y <= 0.0f) sz.y = ui::metrics::reBtnH();

    ImGui::PushStyleColor(ImGuiCol_Button,        base);
    ImGui::PushStyleColor(ImGuiCol_ButtonHovered, hovered);
    ImGui::PushStyleColor(ImGuiCol_ButtonActive,  active);
    ImGui::PushStyleColor(ImGuiCol_Text,          text);
    ImGui::PushStyleVar(ImGuiStyleVar_FrameRounding, ui::metrics::reChipRound());

    if (!enabled) ImGui::BeginDisabled();
    bool pressed = ImGui::Button(label ? label : "", sz);
    if (!enabled) {
        ImGui::EndDisabled();
        pressed = false;
        // Disabled items do not report hover normally; AllowWhenDisabled is what makes the
        // explanation reachable at all.
        if (ImGui::IsItemHovered(ImGuiHoveredFlags_AllowWhenDisabled)) kitTooltip(disabledReason);
    }

    ImGui::PopStyleVar();
    ImGui::PopStyleColor(4);
    return pressed;
}

// ---- centred text helpers (EmptyState / BlockedState) -------------------------------------------
void centeredText(const char* s, float rowLeft, float availW, const ImVec4& col) {
    if (!s || !s[0]) return;
    const ImVec2 ts = ImGui::CalcTextSize(s);
    const ImVec2 c  = ImGui::GetCursorScreenPos();
    const float dx  = (availW - ts.x) * 0.5f;
    ImGui::SetCursorScreenPos(ImVec2(rowLeft + (dx > 0.0f ? dx : 0.0f), c.y));
    ImGui::PushStyleColor(ImGuiCol_Text, col);
    ImGui::TextUnformatted(s);
    ImGui::PopStyleColor();
}

void wrappedBlock(const char* s, float leftScreenX, float w, const ImVec4& col) {
    if (!s || !s[0]) return;
    const ImVec2 c = ImGui::GetCursorScreenPos();
    ImGui::SetCursorScreenPos(ImVec2(leftScreenX, c.y));
    ImGui::PushStyleColor(ImGuiCol_Text, col);
    ImGui::PushTextWrapPos(ImGui::GetCursorPosX() + w);
    ImGui::TextUnformatted(s);
    ImGui::PopTextWrapPos();
    ImGui::PopStyleColor();
}

// ---- thumbnail quality badge --------------------------------------------------------------------
Level badgeLevel(const char* badge) {
    if (!badge || !badge[0]) return Level::Neutral;
    if (std::strcmp(badge, "ok") == 0)        return Level::Ok;
    if (std::strcmp(badge, "blurred") == 0)   return Level::Warn;
    if (std::strcmp(badge, "duplicate") == 0) return Level::Error;
    if (std::strcmp(badge, "synthetic") == 0) return Level::Warn;
    return Level::Neutral;
}

const char* badgeShort(const char* badge) {
    if (!badge || !badge[0]) return "";
    if (std::strcmp(badge, "blurred") == 0)   return "blur";
    if (std::strcmp(badge, "duplicate") == 0) return "dup";
    if (std::strcmp(badge, "synthetic") == 0) return "synth";
    return badge;
}

}  // namespace

// =================================================================================================
// semantic status
// =================================================================================================
ImU32 levelColor(Level l) { return roleU32(levelRole(l)); }

ImVec4 levelColorVec(Level l) { return roleVec(levelRole(l)); }

// =================================================================================================
// cards
// =================================================================================================
bool CardBegin(const char* id, const char* title, const char* icon, bool collapsible, bool* open) {
    ImGui::PushID(id ? id : "re_card");

    // Supplying `open` implies the card can collapse, even if the caller left `collapsible` false.
    const bool canCollapse = collapsible || open != nullptr;

    // Resolve the open state BEFORE BeginChild so the fallback storage (and its key) belong to the
    // PARENT window and survive the child's own id-stack reset.
    bool          isOpen   = true;
    ImGuiStorage* store    = nullptr;
    ImGuiID       stateKey = 0;
    if (canCollapse) {
        if (open) {
            isOpen = *open;
        } else {
            store    = ImGui::GetStateStorage();
            stateKey = ImGui::GetID("re_card_open");
            isOpen   = store->GetBool(stateKey, true);
        }
    }

    ImGui::PushStyleColor(ImGuiCol_ChildBg, roleVec(palette.glassBg, 0.0f));  // we paint our own
    ImGui::PushStyleColor(ImGuiCol_Border,  roleVec(palette.glassBorder));
    ImGui::PushStyleVar(ImGuiStyleVar_ChildRounding,   ui::metrics::reCardRound());
    ImGui::PushStyleVar(ImGuiStyleVar_ChildBorderSize, ui::metrics::stroke(1.0f));
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding,
                        ImVec2(ui::metrics::reCardPadX(), ui::metrics::reCardPadY()));
    ImGui::BeginChild("re_card_body", ImVec2(0.0f, 0.0f),
                      ImGuiChildFlags_AutoResizeY | ImGuiChildFlags_Borders);
    // Content draws on channel 1; CardEnd paints the liquid-glass wash behind it on channel 0 once
    // the auto-resized height is final. Split + merge are unconditional and paired.
    ImGui::GetWindowDrawList()->ChannelsSplit(2);
    ImGui::GetWindowDrawList()->ChannelsSetCurrent(1);

    CardFrame f;
    f.styleVars    = 3;
    f.styleColors  = 2;
    f.contentLeft  = ImGui::GetCursorScreenPos().x;
    f.contentRight = f.contentLeft + ImGui::GetContentRegionAvail().x;
    f.open         = isOpen;
    g_cards.push_back(f);

    const bool wantHeader = (title && title[0]) || (icon && icon[0]) || canCollapse;
    if (!wantHeader) return isOpen;

    ImDrawList*  dl     = ImGui::GetWindowDrawList();
    const float  txtH   = ImGui::GetTextLineHeight();
    const float  iconSz = ui::metrics::iconHeader();
    const float  rowH   = (icon && icon[0]) ? ImMax(txtH, iconSz) : txtH;
    const float  gapH   = isOpen ? ui::metrics::reHeadGap() : 0.0f;
    const float  rowW   = ImMax(f.contentRight - f.contentLeft, ui::metrics::stroke(1.0f));
    const ImVec2 rowMin = ImGui::GetCursorScreenPos();

    // The header row is ONE item (its height already carries the head gap) so CardHeaderRight's
    // SameLine rewind lands on the title row rather than on a spacer.
    bool toggled = false;
    if (canCollapse) {
        toggled = ImGui::InvisibleButton("re_card_head", ImVec2(rowW, rowH + gapH));
    } else {
        ImGui::Dummy(ImVec2(rowW, rowH + gapH));
    }
    const bool headHovered = ImGui::IsItemHovered();

    float       x  = rowMin.x;
    const float cy = rowMin.y + rowH * 0.5f;
    if (canCollapse) {
        ImGui::RenderArrow(dl, ImVec2(x, rowMin.y + (rowH - txtH) * 0.5f),
                           roleU32(palette.textDim, headHovered ? 1.0f : kArrowDimA),
                           isOpen ? ImGuiDir_Down : ImGuiDir_Right, 1.0f);
        x += txtH + ui::metrics::reLabelGap();
    }
    if (icon && icon[0]) {
        drawIconAt(dl, icon, ImVec2(x + iconSz * 0.5f, cy), iconSz, roleU32(palette.accent));
        x += iconSz + ui::metrics::reLabelGap();
    }
    if (title && title[0]) {
        dl->AddText(ImVec2(x, rowMin.y + (rowH - txtH) * 0.5f), roleU32(palette.text), title);
        x += ImGui::CalcTextSize(title).x;
    }
    g_cards.back().headerEndX = x + ui::metrics::reLabelGap() * 2.0f;

    if (toggled) {
        isOpen = !isOpen;
        if (open)       *open = isOpen;
        else if (store) store->SetBool(stateKey, isOpen);
        g_cards.back().open = isOpen;
    }
    g_cards.back().hasHeader = true;
    return isOpen;
}

void CardEnd() {
    if (g_cards.empty()) return;   // unmatched End: refuse rather than corrupt the ImGui stacks
    const CardFrame f = g_cards.back();
    g_cards.pop_back();

    ImDrawList*  dl = ImGui::GetWindowDrawList();
    const ImVec2 mn = ImGui::GetWindowPos();
    const ImVec2 sz = ImGui::GetWindowSize();
    const ImVec2 mx(mn.x + sz.x, mn.y + sz.y);
    dl->ChannelsSetCurrent(0);
    const float tint[3] = { palette.accent[0], palette.accent[1], palette.accent[2] };
    ui::glassCardWash(dl, mn, mx, cardRoundDp(), tint, kCardWashA);
    dl->ChannelsMerge();

    ImGui::EndChild();
    ImGui::PopStyleVar(f.styleVars);
    ImGui::PopStyleColor(f.styleColors);
    ImGui::PopID();
    // No trailing gap here on purpose — GapCards() is the explicit between-cards rhythm token.
}

void CardHeaderRight(float widgetsWidth) {
    if (g_cards.empty()) return;
    const CardFrame& f = g_cards.back();
    float x = f.contentRight - widgetsWidth;
    // Floor at the end of the title, NOT at the content edge: clamping to contentLeft when the
    // widget is wider than the free space parked the chip directly on top of the title text.
    // Overflowing to the right (and being clipped by the card) is strictly better than overlap.
    const float floorX = f.headerEndX > 0.0f ? f.headerEndX : f.contentLeft;
    if (x < floorX) x = floorX;
    // SameLine rewinds Y onto the header row; the absolute X avoids any scroll/indent arithmetic.
    if (f.hasHeader) ImGui::SameLine(0.0f, 0.0f);
    ImGui::SetCursorScreenPos(ImVec2(x, ImGui::GetCursorScreenPos().y));
}

void SectionHeader(const char* label, const char* icon) {
    ImDrawList* dl   = ImGui::GetWindowDrawList();
    const float txtH = ImGui::GetTextLineHeight();
    const float rowX = ImGui::GetCursorScreenPos().x;

    if (icon && icon[0]) {
        const ImVec2 p = ImGui::GetCursorScreenPos();
        drawIconAt(dl, icon, ImVec2(p.x + txtH * 0.5f, p.y + txtH * 0.5f), txtH,
                   roleU32(palette.accent));
        ImGui::Dummy(ImVec2(txtH, txtH));
        ImGui::SameLine(0.0f, ui::metrics::reLabelGap());
    }
    ImGui::PushStyleColor(ImGuiCol_Text, roleVec(palette.accent));
    ImGui::TextUnformatted(label ? label : "");
    ImGui::PopStyleColor();

    const float rowBottom = ImGui::GetItemRectMax().y;
    const float rule      = ui::metrics::stroke(1.0f);
    const float rightX    = ImGui::GetCursorScreenPos().x + ImGui::GetContentRegionAvail().x;
    dl->AddLine(ImVec2(rowX, rowBottom + rule), ImVec2(rightX, rowBottom + rule),
                roleU32(palette.accent, kRuleA), rule);
    ImGui::Dummy(ImVec2(0.0f, ui::metrics::reHeadGap()));
}

// =================================================================================================
// chips / badges
// =================================================================================================
void StatusChip(const char* label, Level l, const char* tooltip) {
    const ChipGeom g  = chipGeom(label, /*withDot=*/true);
    const ImVec2   p0 = ImGui::GetCursorScreenPos();
    ImGui::Dummy(ImVec2(g.w, g.h));
    const bool hovered = ImGui::IsItemHovered();

    ImDrawList*  dl = ImGui::GetWindowDrawList();
    const ImVec2 p1(p0.x + g.w, p0.y + g.h);
    const float* role = levelRole(l);
    const float  rnd  = ui::metrics::reChipRound();
    dl->AddRectFilled(p0, p1, roleU32(role, kChipFillA), rnd);
    dl->AddRect(p0, p1, roleU32(role, hovered ? 1.0f : kChipBorderA), rnd, 0,
                ui::metrics::stroke(1.0f));
    drawLevelDot(dl, ImVec2(p0.x + g.padX + g.dotR, p0.y + g.h * 0.5f), g.dotR, l);
    dl->AddText(ImVec2(p0.x + g.padX + g.dotR * 2.0f + g.gap, p0.y + g.padY), roleU32(role),
                label ? label : "");
    if (hovered) kitTooltip(tooltip);
}

void ValueChip(const char* label, const char* value, Level l, const char* tooltip) {
    const char*  lab  = label ? label : "";
    const char*  val  = value ? value : "";
    const float  padX = ui::metrics::U(1.5f);
    const float  padY = ui::metrics::U(0.5f);
    const float  gap  = ui::metrics::U(1.0f);
    const ImVec2 ls   = ImGui::CalcTextSize(lab);
    const ImVec2 vs   = ImGui::CalcTextSize(val);
    const float  h    = ImMax(ls.y, vs.y) + padY * 2.0f;
    const float  w    = padX * 2.0f + ls.x + gap + vs.x;

    const ImVec2 p0 = ImGui::GetCursorScreenPos();
    ImGui::Dummy(ImVec2(w, h));
    const bool hovered = ImGui::IsItemHovered();

    ImDrawList*  dl = ImGui::GetWindowDrawList();
    const ImVec2 p1(p0.x + w, p0.y + h);
    const float* role = levelRole(l);
    const float  rnd  = ui::metrics::reChipRound();
    dl->AddRectFilled(p0, p1, roleU32(role, kChipFillA), rnd);
    dl->AddRect(p0, p1, roleU32(role, hovered ? 1.0f : kChipBorderA), rnd, 0,
                ui::metrics::stroke(1.0f));
    dl->AddText(ImVec2(p0.x + padX, p0.y + (h - ls.y) * 0.5f), roleU32(palette.textDim), lab);
    dl->AddText(ImVec2(p0.x + padX + ls.x + gap, p0.y + (h - vs.y) * 0.5f), roleU32(role), val);
    if (hovered) kitTooltip(tooltip);
}

void HonestyTag(const char* tier, const char* detail) {
    const char* t = (tier && tier[0]) ? tier : "unknown";

    Level       l   = Level::Neutral;
    const char* why = "Provenance was not recorded for this value — treat it as unverified.";
    if (std::strcmp(t, "exact") == 0) {
        l   = Level::Ok;
        why = "Exact — read directly from a measured, dimensioned source (datasheet table, "
              "drawing or CAD), not from reconstructed geometry.";
    } else if (std::strcmp(t, "high") == 0) {
        l   = Level::Ok;
        why = "High confidence — measured on a reconstruction whose scale was set from a verified "
              "reference and whose fit residual is within tolerance.";
    } else if (std::strcmp(t, "medium") == 0) {
        l   = Level::Warn;
        why = "Medium confidence — measured on a scaled reconstruction, but the scale reference or "
              "the fit residual is weak. Verify before machining.";
    } else if (std::strcmp(t, "low") == 0) {
        l   = Level::Error;
        why = "Low confidence — photo-derived or mesh-fallback geometry with no reliable scale. "
              "Do not machine from this value without measuring the physical part.";
    } else if (std::strcmp(t, "approximate") == 0) {
        l   = Level::Warn;
        why = "Approximate — estimated from unscaled or partial data. The shape is indicative, the "
              "number is not a measurement.";
    }

    const ChipGeom g  = chipGeom(t, /*withDot=*/false);
    const ImVec2   p0 = ImGui::GetCursorScreenPos();
    ImGui::Dummy(ImVec2(g.w, g.h));
    const bool hovered = ImGui::IsItemHovered();

    ImDrawList*  dl = ImGui::GetWindowDrawList();
    const ImVec2 p1(p0.x + g.w, p0.y + g.h);
    const float* role = levelRole(l);
    const float  rnd  = ui::metrics::reChipRound();
    // Solid-bordered pill (no dot) so an honesty tier never reads as a transient status chip.
    dl->AddRectFilled(p0, p1, roleU32(role, kChipFillA), rnd);
    dl->AddRect(p0, p1, roleU32(role, 1.0f), rnd, 0, ui::metrics::stroke(1.0f));
    dl->AddText(ImVec2(p0.x + g.padX, p0.y + g.padY), roleU32(role), t);

    if (hovered) {
        std::string msg = why;
        if (detail && detail[0]) {
            msg += "\n\n";
            msg += detail;
        }
        kitTooltip(msg.c_str());
    }
}

// =================================================================================================
// inline feedback
// =================================================================================================
void InlineStatus(const char* msg, Level l) {
    if (!msg || !msg[0]) return;   // sticky-error callers pass an empty string unconditionally

    ImDrawList*  dl   = ImGui::GetWindowDrawList();
    const float  dotR = ui::metrics::U(0.75f);
    const float  txtH = ImGui::GetTextLineHeight();
    const ImVec2 p    = ImGui::GetCursorScreenPos();
    drawLevelDot(dl, ImVec2(p.x + dotR, p.y + txtH * 0.5f), dotR, l);
    ImGui::Dummy(ImVec2(dotR * 2.0f, txtH));
    ImGui::SameLine(0.0f, ui::metrics::reLabelGap());

    ImGui::PushStyleColor(ImGuiCol_Text, levelColorVec(l));
    ImGui::TextWrapped("%s", msg);
    ImGui::PopStyleColor();
}

void HelpMarker(const char* text) {
    ImGui::PushStyleColor(ImGuiCol_Text, roleVec(palette.textDim));
    ImGui::TextUnformatted("(?)");
    ImGui::PopStyleColor();
    if (ImGui::IsItemHovered()) kitTooltip(text);
}

// =================================================================================================
// form rows
// =================================================================================================
void FieldLabel(const char* label, const char* help) {
    const float rowLeft = ImGui::GetCursorScreenPos().x;

    ImGui::PushStyleColor(ImGuiCol_Text, roleVec(palette.textDim));
    ImGui::TextUnformatted(label ? label : "");
    ImGui::PopStyleColor();
    if (help && help[0]) {
        ImGui::SameLine(0.0f, ui::metrics::reLabelGap());
        HelpMarker(help);
    }

    float       x    = rowLeft + ui::metrics::pmLabelCol();
    const float minX = ImGui::GetItemRectMax().x + ImGui::GetStyle().ItemSpacing.x;
    if (x < minX) x = minX;
    ImGui::SameLine(0.0f, 0.0f);
    ImGui::SetCursorScreenPos(ImVec2(x, ImGui::GetCursorScreenPos().y));
    ImGui::SetNextItemWidth(ui::metrics::reFieldW());
}

void KeyValueRow(const char* key, const char* value, Level valueLevel) {
    const float rowLeft = ImGui::GetCursorScreenPos().x;

    ImGui::PushStyleColor(ImGuiCol_Text, roleVec(palette.textDim));
    ImGui::TextUnformatted(key ? key : "");
    ImGui::PopStyleColor();

    float       x    = rowLeft + ui::metrics::pmLabelCol();
    const float minX = ImGui::GetItemRectMax().x + ImGui::GetStyle().ItemSpacing.x;
    if (x < minX) x = minX;
    ImGui::SameLine(0.0f, 0.0f);
    ImGui::SetCursorScreenPos(ImVec2(x, ImGui::GetCursorScreenPos().y));

    // Neutral reads as PRIMARY text here: the key is already dim, so dimming the value too would
    // make a perfectly good readout look disabled.
    ImGui::PushStyleColor(ImGuiCol_Text,
                          valueLevel == Level::Neutral ? roleVec(palette.text)
                                                       : levelColorVec(valueLevel));
    ImGui::PushTextWrapPos(0.0f);
    ImGui::TextUnformatted(value ? value : "");
    ImGui::PopTextWrapPos();
    ImGui::PopStyleColor();
}

// =================================================================================================
// buttons
// =================================================================================================
bool PrimaryButton(const char* label, bool enabled, const char* disabledReason,
                   const ImVec2& size) {
    const ImVec4 base = roleVec(palette.accent, kPrimaryFillA);
    return kitButton(label, enabled, disabledReason, size, base, roleVec(palette.accent),
                     roleVec(palette.accentDim), ui::maxContrastTextOn(base));
}

bool SecondaryButton(const char* label, bool enabled, const char* disabledReason,
                     const ImVec2& size) {
    return kitButton(label, enabled, disabledReason, size, roleVec(palette.button),
                     roleVec(palette.buttonHovered), roleVec(palette.buttonActive),
                     roleVec(palette.text));
}

bool DangerButton(const char* label, bool enabled, const char* disabledReason) {
    const ImVec4 base = roleVec(palette.statusErr, kDangerFillA);
    return kitButton(label, enabled, disabledReason, ImVec2(0.0f, ui::metrics::reBtnH()), base,
                     roleVec(palette.statusErr), roleVec(palette.statusErr, kDangerActA),
                     ui::maxContrastTextOn(base));
}

bool SegmentedControl(const char* id, const char* const* labels, int count, int* selected,
                      float width) {
    if (!labels || count <= 0 || !selected) return false;

    ImGui::PushID(id ? id : "re_seg");
    const float  h    = ui::metrics::reBtnH();
    float        w    = width > 0.0f ? width : ImGui::GetContentRegionAvail().x;
    const float  minW = ui::metrics::stroke(1.0f) * (float)count;
    if (w < minW) w = minW;
    const float  segW = w / (float)count;
    const ImVec2 p0   = ImGui::GetCursorScreenPos();
    const ImVec2 p1(p0.x + w, p0.y + h);

    ImDrawList* dl  = ImGui::GetWindowDrawList();
    const float rnd = ui::metrics::reChipRound();
    dl->AddRectFilled(p0, p1, roleU32(palette.frameBg, kTrackA), rnd);
    dl->AddRect(p0, p1, roleU32(palette.border), rnd, 0, ui::metrics::stroke(1.0f));

    const float inset   = ui::metrics::stroke(2.0f);
    bool        changed = false;
    for (int i = 0; i < count; ++i) {
        const ImVec2 sMin(p0.x + segW * (float)i, p0.y);
        const ImVec2 sMax(sMin.x + segW, p1.y);

        ImGui::PushID(i);
        ImGui::SetCursorScreenPos(sMin);
        if (ImGui::InvisibleButton("seg", ImVec2(segW, h)) && *selected != i) {
            *selected = i;
            changed   = true;
        }
        const bool hovered = ImGui::IsItemHovered();
        ImGui::PopID();

        const bool sel = (*selected == i);
        if (sel) {
            dl->AddRectFilled(ImVec2(sMin.x + inset, sMin.y + inset),
                              ImVec2(sMax.x - inset, sMax.y - inset),
                              roleU32(palette.accent, kSegSelA), rnd);
        } else if (hovered) {
            dl->AddRectFilled(ImVec2(sMin.x + inset, sMin.y + inset),
                              ImVec2(sMax.x - inset, sMax.y - inset),
                              roleU32(palette.accent, kSegHoverA), rnd);
        } else if (i > 0) {
            dl->AddLine(ImVec2(sMin.x, sMin.y + inset * 2.0f),
                        ImVec2(sMin.x, sMax.y - inset * 2.0f), roleU32(palette.border),
                        ui::metrics::stroke(1.0f));
        }

        // Same fit-before-centre rule as StepTrack: clipping a centred label eats both ends.
        const std::string lab = ellipsize(labels[i] ? labels[i] : "", segW - inset * 2.0f);
        const ImVec2      ts  = ImGui::CalcTextSize(lab.c_str());
        const ImU32       tc  = sel ? ImGui::ColorConvertFloat4ToU32(
                                          ui::maxContrastTextOn(roleVec(palette.accent, kSegSelA)))
                                    : roleU32(hovered ? palette.text : palette.textDim);
        dl->PushClipRect(ImVec2(sMin.x + inset, sMin.y), ImVec2(sMax.x - inset, sMax.y), true);
        dl->AddText(ImVec2(sMin.x + (segW - ts.x) * 0.5f, sMin.y + (h - ts.y) * 0.5f), tc,
                    lab.c_str());
        dl->PopClipRect();
        if (hovered && labels[i] && lab != labels[i]) ImGui::SetTooltip("%s", labels[i]);
    }

    ImGui::SetCursorScreenPos(p0);
    ImGui::Dummy(ImVec2(w, h));
    ImGui::PopID();
    return changed;
}

// =================================================================================================
// progress
// =================================================================================================
void ProgressBar(float pct, const char* caption, Level l) {
    const float availW = ImGui::GetContentRegionAvail().x;
    const float barH   = ui::metrics::reProgressH();
    const bool  hasCap = caption && caption[0];

    ImVec2 capSz(0.0f, 0.0f);
    float  capGap = 0.0f;
    if (hasCap) {
        capSz  = ImGui::CalcTextSize(caption);
        capGap = ui::metrics::reCtlGap();
    }
    const float rowH = ImMax(barH, capSz.y);
    float       barW = availW - capSz.x - capGap;
    const float minW = ui::metrics::stroke(1.0f);
    if (barW < minW) barW = minW;

    const ImVec2 p0 = ImGui::GetCursorScreenPos();
    ImGui::Dummy(ImVec2(availW, rowH));

    ImDrawList* dl  = ImGui::GetWindowDrawList();
    const float by  = p0.y + (rowH - barH) * 0.5f;
    const float rnd = barH * 0.5f;
    dl->AddRectFilled(ImVec2(p0.x, by), ImVec2(p0.x + barW, by + barH),
                      roleU32(palette.frameBg, kTrackA), rnd);

    const ImU32 fill = (l == Level::Neutral) ? roleU32(palette.accent) : levelColor(l);
    if (pct < 0.0f) {
        // Indeterminate: a marquee segment sweeping the track. A started job with no reported
        // progress still LOOKS like it is running.
        const float segW = barW * kMarqueeFrac;
        const float span = barW + segW;
        const float t    = (float)ImGui::GetTime() * kMarqueeSpeed;
        const float head = (t - std::floor(t)) * span - segW;
        const float a    = p0.x + ImMax(0.0f, head);
        const float b    = p0.x + ImMin(barW, head + segW);
        if (b > a) dl->AddRectFilled(ImVec2(a, by), ImVec2(b, by + barH), fill, rnd);
    } else {
        const float f = pct > 1.0f ? 1.0f : pct;
        const float w = barW * f;
        if (w > 0.0f) dl->AddRectFilled(ImVec2(p0.x, by), ImVec2(p0.x + w, by + barH), fill, rnd);
    }

    if (hasCap) {
        dl->AddText(ImVec2(p0.x + availW - capSz.x, p0.y + (rowH - capSz.y) * 0.5f),
                    (l == Level::Neutral) ? roleU32(palette.textDim) : levelColor(l), caption);
    }
}

int StepTrack(const char* id, const char* const* labels, const Level* states, int count,
              int active, bool clickable, float height) {
    if (!labels || count <= 0) return -1;

    ImGui::PushID(id ? id : "re_steps");
    const float  availW = ImGui::GetContentRegionAvail().x;
    float        h      = height;
    if (h <= 0.0f) h = ui::metrics::reStepperH();
    const float  segW   = availW / (float)count;
    const float  nodeR  = ui::metrics::U(2.0f);
    const float  txtH   = ImGui::GetTextLineHeight();
    const float  labGap = ui::metrics::reLabelGap();
    const ImVec2 p0     = ImGui::GetCursorScreenPos();

    // Node row sits above the label row, the pair vertically centred in `h`.
    const float blockH = nodeR * 2.0f + labGap + txtH;
    const float top    = p0.y + ImMax(0.0f, (h - blockH) * 0.5f);
    const float nodeY  = top + nodeR;
    const float labY   = top + nodeR * 2.0f + labGap;

    ImDrawList* dl = ImGui::GetWindowDrawList();

    // Connectors first so the nodes sit on top of them.
    for (int i = 0; i + 1 < count; ++i) {
        const Level lv = states ? states[i] : Level::Neutral;
        const float x0 = p0.x + segW * ((float)i + 0.5f) + nodeR;
        const float x1 = p0.x + segW * ((float)i + 1.5f) - nodeR;
        if (x1 <= x0) continue;
        dl->AddLine(ImVec2(x0, nodeY), ImVec2(x1, nodeY),
                    (lv == Level::Ok) ? levelColor(Level::Ok) : roleU32(palette.border),
                    ui::metrics::stroke(1.5f));
    }

    int clicked = -1;
    for (int i = 0; i < count; ++i) {
        const ImVec2 sMin(p0.x + segW * (float)i, p0.y);
        bool         hovered = false;
        if (clickable) {
            ImGui::PushID(i);
            ImGui::SetCursorScreenPos(sMin);
            if (ImGui::InvisibleButton("step", ImVec2(ImMax(segW, ui::metrics::stroke(1.0f)), h)))
                clicked = i;
            hovered = ImGui::IsItemHovered();
            ImGui::PopID();
        }

        const Level  lv = states ? states[i] : Level::Neutral;
        const float* rl = levelRole(lv);
        const ImVec2 c(sMin.x + segW * 0.5f, nodeY);

        if (i == active) {
            const float pulse =
                kPulseBase + kPulseAmp * (float)std::sin(ImGui::GetTime() * kPulseSpeed);
            dl->AddCircleFilled(c, nodeR * pulse, roleU32(palette.accent, kNodeHaloA));
        }
        dl->AddCircleFilled(c, nodeR, roleU32(rl, lv == Level::Neutral ? kNodeIdleA : 1.0f));
        dl->AddCircle(c, nodeR, roleU32(rl, hovered ? 1.0f : kChipBorderA), 0,
                      ui::metrics::stroke(1.0f));
        if (lv == Level::Busy) {
            drawSpinner(dl, c, nodeR + ui::metrics::stroke(2.5f), ui::metrics::stroke(1.5f),
                        levelColor(Level::Busy));
        }

        // Fit the label to its own segment (minus a hair of breathing room) BEFORE centring.
        // Centre-then-clip would trim both ends and leave an unreadable middle fragment.
        const std::string lab = ellipsize(labels[i] ? labels[i] : "",
                                          segW - ui::metrics::U(1.0f));
        const ImVec2      ts  = ImGui::CalcTextSize(lab.c_str());
        dl->PushClipRect(ImVec2(sMin.x, p0.y), ImVec2(sMin.x + segW, p0.y + h), true);
        dl->AddText(ImVec2(c.x - ts.x * 0.5f, labY),
                    roleU32((i == active || hovered) ? palette.text : palette.textDim),
                    lab.c_str());
        dl->PopClipRect();
        // Full text on hover, so an ellipsized step is never ambiguous.
        if (hovered && labels[i] && lab != labels[i]) ImGui::SetTooltip("%s", labels[i]);
    }

    ImGui::SetCursorScreenPos(p0);
    ImGui::Dummy(ImVec2(availW, h));
    ImGui::PopID();
    return clicked;
}

// =================================================================================================
// previews
// =================================================================================================
bool HeroPreview(const char* id, unsigned int texId, int texW, int texH, const ImVec2& box,
                 const char* caption, const char* emptyLabel) {
    ImGui::PushID(id ? id : "re_hero");

    ImVec2      b    = box;
    const float minSz = ui::metrics::reThumbSm();
    if (b.x <= 0.0f) b.x = ImGui::GetContentRegionAvail().x;
    if (b.x < minSz) b.x = minSz;
    if (b.y <= 0.0f) b.y = b.x * kHeroAspect;
    if (b.y < minSz) b.y = minSz;

    const ImVec2 p0      = ImGui::GetCursorScreenPos();
    const bool   clicked = ImGui::InvisibleButton("hero", b);
    const bool   hovered = ImGui::IsItemHovered();
    const ImVec2 p1(p0.x + b.x, p0.y + b.y);

    ImDrawList* dl = ImGui::GetWindowDrawList();
    ui::glassBackground(dl, p0, p1, cardRoundDp());

    const float inset = ui::metrics::U(1.0f);
    const float txtH  = ImGui::GetTextLineHeight();
    const float capH  = (caption && caption[0]) ? (txtH + ui::metrics::U(1.0f)) : 0.0f;
    const ImVec2 i0(p0.x + inset, p0.y + inset);
    const ImVec2 i1(p1.x - inset, p1.y - inset - capH);

    if (texId != 0) {
        drawLetterboxed(dl, (ImTextureID)(uintptr_t)texId, texW, texH, i0, i1);
    } else {
        drawPlaceholder(dl, i0, i1, emptyLabel ? emptyLabel : "no preview yet");
    }

    if (capH > 0.0f) {
        const ImVec2 c0(p0.x + inset, p1.y - inset - capH);
        const ImVec2 c1(p1.x - inset, p1.y - inset);
        dl->AddRectFilled(c0, c1, roleU32(palette.glassBg, kStripA), ui::metrics::reChipRound());
        dl->PushClipRect(c0, c1, true);
        dl->AddText(ImVec2(c0.x + ui::metrics::U(1.0f), c0.y + (capH - txtH) * 0.5f),
                    roleU32(palette.text), caption);
        dl->PopClipRect();
    }

    if (hovered) {
        dl->AddRect(p0, p1, roleU32(palette.accent, kHoverRingA), ui::metrics::reCardRound(), 0,
                    ui::metrics::stroke(1.5f));
    }
    ImGui::PopID();
    return clicked;
}

bool ThumbTile(const char* id, unsigned int texId, float side, bool selected, const char* badge,
               const char* label) {
    ImGui::PushID(id ? id : "re_thumb");
    const float s = side > 0.0f ? side : ui::metrics::reThumbMd();

    const ImVec2 p0      = ImGui::GetCursorScreenPos();
    const bool   clicked = ImGui::InvisibleButton("tile", ImVec2(s, s));
    const bool   hovered = ImGui::IsItemHovered();
    const ImVec2 p1(p0.x + s, p0.y + s);

    ImDrawList* dl  = ImGui::GetWindowDrawList();
    const float rnd = ui::metrics::reChipRound();
    dl->AddRectFilled(p0, p1, roleU32(palette.glassBg, hovered ? kTileHoverA : kTileBaseA), rnd);

    const float  inset = ui::metrics::stroke(2.0f);
    const ImVec2 i0(p0.x + inset, p0.y + inset);
    const ImVec2 i1(p1.x - inset, p1.y - inset);
    if (texId != 0) {
        drawLetterboxed(dl, (ImTextureID)(uintptr_t)texId, 0, 0, i0, i1);
    } else {
        drawPlaceholder(dl, i0, i1, label ? label : "");
    }

    // Index / name strip along the bottom (only when it cannot swallow the thumbnail).
    const float txtH  = ImGui::GetTextLineHeight();
    const float stripH = txtH + ui::metrics::U(0.5f) * 2.0f;
    if (texId != 0 && label && label[0] && s > stripH * 2.0f) {
        const ImVec2 s0(i0.x, i1.y - stripH);
        dl->AddRectFilled(s0, i1, roleU32(palette.glassBg, kStripA), rnd);
        dl->PushClipRect(s0, i1, true);
        dl->AddText(ImVec2(s0.x + ui::metrics::U(0.5f), s0.y + ui::metrics::U(0.5f)),
                    roleU32(palette.text), label);
        dl->PopClipRect();
    }

    // Quality badge, top-right corner of the tile.
    if (badge && badge[0]) {
        const Level    bl = badgeLevel(badge);
        const char*    bs = badgeShort(badge);
        const ChipGeom bg = chipGeom(bs, /*withDot=*/false);
        const ImVec2   b1(i1.x, i0.y + bg.h);
        const ImVec2   b0(b1.x - bg.w, i0.y);
        if (bg.w < (i1.x - i0.x)) {
            dl->AddRectFilled(b0, b1, levelColor(bl), rnd);
            dl->AddText(ImVec2(b0.x + bg.padX, b0.y + bg.padY),
                        ImGui::ColorConvertFloat4ToU32(ui::maxContrastTextOn(levelColorVec(bl))),
                        bs);
        }
    }

    if (selected) {
        ui::glassSelectionGlow(dl, p0, p1, chipRoundDp(), palette.accent);
    } else if (hovered) {
        dl->AddRect(p0, p1, roleU32(palette.accent, kHoverRingA), rnd, 0,
                    ui::metrics::stroke(1.0f));
    }

    ImGui::PopID();
    return clicked;
}

void ThumbHoverPreview(unsigned int texId, int texW, int texH,
                       const std::vector<std::string>& metaLines) {
    if (!ImGui::BeginTooltip()) return;

    const float w = ui::metrics::rePreviewMinW();
    float       aspect = (texW > 0 && texH > 0) ? ((float)texW / (float)texH) : 1.0f;
    if (!(aspect > 0.0f)) aspect = 1.0f;
    float       h    = w / aspect;
    const float hMax = ui::metrics::rePreviewMaxW() * kHeroAspect;
    if (h > hMax) h = hMax;
    if (h < ui::metrics::reThumbSm()) h = ui::metrics::reThumbSm();

    const ImVec2 p0 = ImGui::GetCursorScreenPos();
    ImGui::Dummy(ImVec2(w, h));
    ImDrawList*  dl = ImGui::GetWindowDrawList();
    const ImVec2 p1(p0.x + w, p0.y + h);
    if (texId != 0) {
        drawLetterboxed(dl, (ImTextureID)(uintptr_t)texId, texW, texH, p0, p1);
    } else {
        drawPlaceholder(dl, p0, p1, "no preview");
    }

    if (!metaLines.empty()) {
        ImGui::Dummy(ImVec2(0.0f, ui::metrics::reLabelGap()));
        ImGui::PushStyleColor(ImGuiCol_Text, roleVec(palette.textDim));
        for (const std::string& line : metaLines) ImGui::TextUnformatted(line.c_str());
        ImGui::PopStyleColor();
    }
    ImGui::EndTooltip();
}

ImRect Viewport3DPreview(const char* id, const ImVec2& box, PreviewSlot slot,
                         const char* emptyLabel) {
    ImGui::PushID(id ? id : "re_vp3d");

    ImVec2      b     = box;
    const float minSz = ui::metrics::reThumbSm();
    if (b.x <= 0.0f) b.x = ImGui::GetContentRegionAvail().x;
    if (b.x < minSz) b.x = minSz;
    if (b.y <= 0.0f) b.y = b.x * kHeroAspect;
    if (b.y < minSz) b.y = minSz;

    const ImVec2 p0 = ImGui::GetCursorScreenPos();
    ImGui::Dummy(b);
    const ImVec2 p1(p0.x + b.x, p0.y + b.y);

    ImDrawList* dl = ImGui::GetWindowDrawList();
    ui::glassBackground(dl, p0, p1, cardRoundDp());
    if (emptyLabel && emptyLabel[0]) {
        const float inset = ui::metrics::U(1.0f);
        drawPlaceholder(dl, ImVec2(p0.x + inset, p0.y + inset), ImVec2(p1.x - inset, p1.y - inset),
                        emptyLabel);
    }

    const ImRect r(p0, p1);
    SlotRec&     rec = g_slots[slotIndex(slot)];
    rec.rect  = r;
    rec.drawn = true;

    ImGui::PopID();
    return r;
}

bool previewSlotRect(PreviewSlot slot, ImRect& out) {
    const SlotRec& rec = g_slots[slotIndex(slot)];
    if (!rec.drawn) return false;
    out = rec.rect;
    return true;
}

void resetPreviewSlots() {
    for (int i = 0; i < kSlotCount; ++i) g_slots[i].drawn = false;
}

// =================================================================================================
// empty / error states
// =================================================================================================
bool EmptyState(const char* icon, const char* title, const char* body, const char* actionLabel,
                bool actionEnabled) {
    ImGui::PushID(title && title[0] ? title : "re_empty");

    const float rowLeft = ImGui::GetCursorScreenPos().x;
    const float availW  = ImGui::GetContentRegionAvail().x;
    const float colW    = ImMin(availW, ui::metrics::rePreviewMinW());
    const float colLeft = rowLeft + ImMax(0.0f, (availW - colW) * 0.5f);

    ImGui::Dummy(ImVec2(0.0f, ui::metrics::reSectionGap()));
    if (icon && icon[0]) {
        const float  sz = ui::metrics::iconHeader() * kEmptyIconScale;
        const ImVec2 p  = ImGui::GetCursorScreenPos();
        drawIconAt(ImGui::GetWindowDrawList(), icon, ImVec2(rowLeft + availW * 0.5f, p.y + sz * 0.5f),
                   sz, roleU32(palette.textDim));
        ImGui::Dummy(ImVec2(availW, sz));
        ImGui::Dummy(ImVec2(0.0f, ui::metrics::reCtlGap()));
    }

    centeredText(title, rowLeft, availW, roleVec(palette.text));
    if (body && body[0]) {
        ImGui::Dummy(ImVec2(0.0f, ui::metrics::reLabelGap()));
        wrappedBlock(body, colLeft, colW, roleVec(palette.textDim));
    }

    bool clicked = false;
    if (actionLabel && actionLabel[0]) {
        ImGui::Dummy(ImVec2(0.0f, ui::metrics::reCtlGap()));
        const float bw = ImMax(ui::metrics::reBtnW(),
                               ImGui::CalcTextSize(actionLabel).x + ui::metrics::reCardPadX() * 2.0f);
        const ImVec2 c = ImGui::GetCursorScreenPos();
        ImGui::SetCursorScreenPos(ImVec2(rowLeft + ImMax(0.0f, (availW - bw) * 0.5f), c.y));
        clicked = PrimaryButton(actionLabel, actionEnabled, nullptr,
                                ImVec2(bw, ui::metrics::reBtnH()));
    }

    ImGui::Dummy(ImVec2(0.0f, ui::metrics::reSectionGap()));
    ImGui::PopID();
    return clicked;
}

bool BlockedState(const char* title, const char* reason, const char* hint,
                  const char* actionLabel) {
    ImGui::PushID(title && title[0] ? title : "re_blocked");

    const float rowLeft = ImGui::GetCursorScreenPos().x;
    const float availW  = ImGui::GetContentRegionAvail().x;
    const float colW    = ImMin(availW, ui::metrics::rePreviewMinW());
    const float colLeft = rowLeft + ImMax(0.0f, (availW - colW) * 0.5f);

    ImGui::Dummy(ImVec2(0.0f, ui::metrics::reSectionGap()));
    {
        const float  sz = ui::metrics::iconHeader() * kEmptyIconScale;
        const ImVec2 p  = ImGui::GetCursorScreenPos();
        drawWarnGlyph(ImGui::GetWindowDrawList(),
                      ImVec2(rowLeft + availW * 0.5f, p.y + sz * 0.5f), sz,
                      levelColor(Level::Error));
        ImGui::Dummy(ImVec2(availW, sz));
        ImGui::Dummy(ImVec2(0.0f, ui::metrics::reCtlGap()));
    }

    centeredText(title, rowLeft, availW, levelColorVec(Level::Error));
    if (reason && reason[0]) {
        ImGui::Dummy(ImVec2(0.0f, ui::metrics::reLabelGap()));
        wrappedBlock(reason, colLeft, colW, roleVec(palette.text));
    }
    if (hint && hint[0]) {
        ImGui::Dummy(ImVec2(0.0f, ui::metrics::reLabelGap()));
        wrappedBlock(hint, colLeft, colW, roleVec(palette.textDim));
    }

    bool clicked = false;
    if (actionLabel && actionLabel[0]) {
        ImGui::Dummy(ImVec2(0.0f, ui::metrics::reCtlGap()));
        const float bw = ImMax(ui::metrics::reBtnW(),
                               ImGui::CalcTextSize(actionLabel).x + ui::metrics::reCardPadX() * 2.0f);
        const ImVec2 c = ImGui::GetCursorScreenPos();
        ImGui::SetCursorScreenPos(ImVec2(rowLeft + ImMax(0.0f, (availW - bw) * 0.5f), c.y));
        clicked = PrimaryButton(actionLabel, true, nullptr, ImVec2(bw, ui::metrics::reBtnH()));
    }

    ImGui::Dummy(ImVec2(0.0f, ui::metrics::reSectionGap()));
    ImGui::PopID();
    return clicked;
}

// =================================================================================================
// capability surfacing
// =================================================================================================
void CapabilityRow(const char* name, bool available, const char* hint) {
    ImGui::PushID(name && name[0] ? name : "re_cap");

    const float rowLeft = ImGui::GetCursorScreenPos().x;
    const float availW  = ImGui::GetContentRegionAvail().x;
    const Level lvl     = available ? Level::Ok : Level::Error;
    const char* chip    = available ? "available" : "missing";

    ImGui::PushStyleColor(ImGuiCol_Text, roleVec(palette.text));
    ImGui::TextUnformatted(name ? name : "");
    ImGui::PopStyleColor();

    const ChipGeom g    = chipGeom(chip, /*withDot=*/true);
    float          x    = rowLeft + availW - g.w;
    const float    minX = ImGui::GetItemRectMax().x + ImGui::GetStyle().ItemSpacing.x;
    if (x < minX) x = minX;
    ImGui::SameLine(0.0f, 0.0f);
    ImGui::SetCursorScreenPos(ImVec2(x, ImGui::GetCursorScreenPos().y));
    StatusChip(chip, lvl, hint);

    // A missing capability's fix-it hint is always VISIBLE, not just a tooltip — that is the whole
    // point of the capability panel (learn COLMAP is missing before clicking Reconstruct).
    if (!available && hint && hint[0]) {
        const float indent = ui::metrics::reCardPadX();
        wrappedBlock(hint, rowLeft + indent, ImMax(availW - indent, indent),
                     roleVec(palette.textDim));
    }
    ImGui::PopID();
}

// =================================================================================================
// misc
// =================================================================================================
void Rule() {
    ImDrawList*  dl = ImGui::GetWindowDrawList();
    const ImVec2 p  = ImGui::GetCursorScreenPos();
    const float  w  = ImGui::GetContentRegionAvail().x;
    const float  t  = ui::metrics::stroke(1.0f);
    dl->AddLine(ImVec2(p.x, p.y + t * 0.5f), ImVec2(p.x + w, p.y + t * 0.5f),
                roleU32(palette.border), t);
    ImGui::Dummy(ImVec2(w, t));
}

void GapCards()   { ImGui::Dummy(ImVec2(0.0f, ui::metrics::reCardGap())); }
void GapSection() { ImGui::Dummy(ImVec2(0.0f, ui::metrics::reSectionGap())); }
void GapControl() { ImGui::Dummy(ImVec2(0.0f, ui::metrics::reCtlGap())); }

std::string fmtMm(double mm, int decimals) {
    if (decimals < 0) decimals = 0;
    if (decimals > kMaxDecimals) decimals = kMaxDecimals;
    char buf[64];
    std::snprintf(buf, sizeof(buf), "%.*f mm", decimals, mm);
    return std::string(buf);
}

std::string fmtPct(double pct01, int decimals) {
    if (decimals < 0) decimals = 0;
    if (decimals > kMaxDecimals) decimals = kMaxDecimals;
    char buf[64];
    std::snprintf(buf, sizeof(buf), "%.*f %%", decimals, pct01 * 100.0);
    return std::string(buf);
}

std::string fmtDuration(double seconds) {
    char buf[64];
    if (!(seconds > 0.0)) {   // also catches NaN
        return std::string("0.0 s");
    }
    const long total = (long)(seconds + 0.5);
    if (total < 60) {
        std::snprintf(buf, sizeof(buf), "%.1f s", seconds);
    } else if (total < 3600) {
        std::snprintf(buf, sizeof(buf), "%ld m %02ld s", total / 60, total % 60);
    } else {
        std::snprintf(buf, sizeof(buf), "%ld h %02ld m", total / 3600, (total % 3600) / 60);
    }
    return std::string(buf);
}

}  // namespace kit
}  // namespace reveng
}  // namespace ui
