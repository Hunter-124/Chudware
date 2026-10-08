// kit.cpp — the ONE implementation of the app-wide widget vocabulary declared in kit.h.
//
// Nothing here is new art. Every widget absorbs the best-behaved of the four per-workspace
// toolkits this file replaces, so the behavior that was already correct somewhere is now correct
// everywhere:
//
//   ui/reveng/reveng_widgets.cpp  card chrome, chips, buttons, segmented control, progress,
//                                 empty/blocked state skeletons, the disabled-button tooltip rule.
//   ui/plm/plm_workspace.cpp      the accent section header WITHOUT an underline rule, and the
//                                 centre-if-fits-else-wrap rule that keeps a long empty-state line
//                                 on screen instead of pushing it off the left edge.
//   ui/plm/plm_pages.cpp          the glassCardWash-backed tinted card (cardBegin's `tint`).
//   ui/copilot/ui_common.cpp      the canonical status glyph (brand mark in motion + vector marks)
//                                 and the semantic-on-glass tone correction.
//   ui/parts/parts_widgets.cpp    the icon / trailing / first-in-pane section-header parameters.
//
// House rules this TU obeys:
//   * Every color is a ui::palette role or an AdaptiveSurface ink. No literal ImU32/ImVec4.
//   * Every dimension comes from the density accessors or ui::metrics::*. No pixel literals.
//   * Every state change (hover, selection, disclosure, chip fill, segment slide, progress fill)
//     eases through ui::anim, which also pumps the frame loop while it moves.
//   * Label strings are ASCII only. The app font has no ellipsis/arrow/check codepoints and
//     renders them as '?', which is why elide() appends three periods rather than U+2026.
//
// GL/GUI thread only.
#include "kit.h"

#include <cmath>
#include <cstdio>
#include <cstring>
#include <ctime>
#include <string>
#include <vector>

#include "imgui.h"
#include "imgui_internal.h"

#include "../anim.h"
#include "../icons.h"
#include "../metrics.h"
#include "../tesseract.h"   // ui::drawTesseractSpinner + kTesseractSpinnerMinPx

namespace ui {
namespace kit {
namespace {

// ---- dp vs. scaled pixels ---------------------------------------------------------------------
// ui::glass*() take a LOGICAL dp radius (they multiply by uiScale themselves); the metrics round
// tokens already return scaled pixels. Convert back exactly once so a corner never double-scales.
inline float dpOf(float scaledPx) {
    const float s = ui::metrics::scale();
    return s > 0.0f ? scaledPx / s : scaledPx;
}

// ---- opacity / ratio constants -----------------------------------------------------------------
// These scale a palette role's alpha or are pure ratios; the COLOR itself is always a role.
constexpr float kCardWashA      = 0.060f;  // tinted card wash at the top edge
constexpr float kCardHoverA     = 0.030f;  // extra wash while the header is hovered
constexpr float kChipFillA      = 0.16f;
constexpr float kChipBorderA    = 0.55f;
constexpr float kChipOffBorderA = 0.28f;   // an unselected toggle chip still shows an edge
constexpr float kChipIdleTextA  = 0.85f;
constexpr float kRowBaseA       = 0.16f;   // resting frosted row wash
constexpr float kRowHoverA      = 0.26f;   // added at full hover
constexpr float kDimA           = 0.62f;
constexpr float kDisabledA      = 0.55f;
constexpr float kMatteA         = 0.85f;   // plate behind a row thumbnail
constexpr float kTrackA         = 0.85f;   // progress / segmented-control track
constexpr float kPrimaryFillA   = 0.90f;
constexpr float kBtnDisabledA   = 0.85f;   // disabled button: neutral frame fill (any button kind)
constexpr float kRuleInkA       = 0.18f;   // rule() hairline: current ink at this alpha
constexpr float kDangerFillA    = 0.85f;
constexpr float kDangerActA     = 0.70f;
constexpr float kSegSelA        = 0.85f;
constexpr float kSegHoverA      = 0.16f;
constexpr float kBtnGhostHoverA = 0.10f;   // icon-button hover wash, over the current ink
constexpr float kEmptyIconA     = 0.60f;
constexpr float kWrapInsetFrac  = 0.08f;   // empty-state paragraph inset when a line must wrap
constexpr float kEmptyIconScale = 2.0f;    // empty/blocked glyph = 2x the header icon tier
constexpr float kMarqueeFrac    = 0.28f;   // indeterminate bar segment, fraction of the track
constexpr float kGlyphRadiusFrac= 0.30f;   // status-mark radius as a fraction of its glyph cell
constexpr float kIconFillFrac   = 0.78f;   // icon-button glyph, at least this fraction of the button
constexpr float kKeyColMaxFrac  = 0.55f;   // a key/value row's key column never exceeds this of the row
constexpr float kSettled        = 0.004f;  // below this a tween is visually done

// ---- palette plumbing ----------------------------------------------------------------------------
inline ImU32 roleU32(const float c[4], float alphaMul = 1.0f) {
    float t[4];
    Palette::withAlpha(c, t, alphaMul);
    return (ImU32)Palette::toU32(t);
}
inline ImVec4 roleVec(const float c[4], float alphaMul = 1.0f) {
    return ImVec4(c[0], c[1], c[2], c[3] * alphaMul);
}
inline ImVec4 lerpVec4(const ImVec4& a, const ImVec4& b, float t) {
    return ImVec4(a.x + (b.x - a.x) * t, a.y + (b.y - a.y) * t,
                  a.z + (b.z - a.z) * t, a.w + (b.w - a.w) * t);
}
inline ImU32 u32(const ImVec4& c) { return ImGui::ColorConvertFloat4ToU32(c); }

const float* toneRole(Tone t) {
    switch (t) {
        case Tone::Accent: return palette.accent;
        case Tone::Ok:     return palette.statusOk;
        case Tone::Warn:   return palette.statusWarn;
        case Tone::Err:    return palette.statusErr;
        case Tone::Info:   return palette.iterBuilt;
        case Tone::Neutral:
        default:           return palette.textDim;
    }
}

// The surface a widget is currently painting on, derived from the ink the caller pushed rather
// than from a fresh adaptivePanel() sample: a kit widget can be drawn inside a copilot glass
// bubble, a docked panel or a popup, and only the caller knows which. Dark ink means a bright
// surface, which is exactly the bit toneOn() needs.
AdaptiveSurface inkSurface() {
    AdaptiveSurface s;
    const ImVec4 ink = ImGui::GetStyleColorVec4(ImGuiCol_Text);
    const float  lum = 0.2126f * ink.x + 0.7152f * ink.y + 0.0722f * ink.z;
    s.darkText      = lum < 0.5f;
    s.textPrimary   = ink;
    s.textSecondary = ImVec4(ink.x, ink.y, ink.z, ink.w * kChipIdleTextA);
    s.textTertiary  = ImVec4(ink.x, ink.y, ink.z, ink.w * kDimA);
    return s;
}

// ---- small painters --------------------------------------------------------------------------
// Wrapped hover tooltip; a no-op on an empty string so callers can pass an optional reason
// straight through. ImGuiCol_PopupBg is already the glass role (applyTheme), so no extra push.
void kitTooltip(const char* text) {
    tooltip(text);  // one tooltip painter: kit_form.cpp kit::tooltip
}

// Draw a caller-named atlas icon centred at `center`. Unknown names draw NOTHING and report it,
// so a caller can fall back rather than reserve space for a hole (the kit never substitutes a
// guessed icon name, and never paints the magenta placeholder tile).
bool drawIconAt(ImDrawList* dl, const char* name, ImVec2 center, float sz, ImU32 col) {
    if (!dl || !name || !name[0] || sz <= 0.0f) return false;
    if (ui::hasIconSdf(name)) {
        ui::drawIconGlass(dl, name, center, sz, col, 0);
        return true;
    }
    const int    cell = (sz >= ui::metrics::iconHeader()) ? 32 : 20;
    const IconUV ic   = ui::getIcon(name, cell);
    if (!ic.valid) return false;
    const float r = sz * 0.5f;
    dl->AddImage(ic.texId, ImVec2(center.x - r, center.y - r), ImVec2(center.x + r, center.y + r),
                 ic.uv0, ic.uv1, col);
    return true;
}

// Hand-drawn warning triangle for blockedState — hand-drawn precisely so the kit does not have to
// hardcode (and possibly outlive) an atlas icon name for its most load-bearing state.
void drawWarnGlyph(ImDrawList* dl, ImVec2 c, float sz, ImU32 col) {
    const float  r    = sz * 0.5f;
    const float  t    = ui::metrics::stroke(1.6f);
    const ImVec2 apex(c.x, c.y - r);
    const ImVec2 bl(c.x - r, c.y + r * 0.72f);
    const ImVec2 br(c.x + r, c.y + r * 0.72f);
    dl->AddTriangle(apex, bl, br, col, t);
    dl->AddLine(ImVec2(c.x, c.y - r * 0.20f), ImVec2(c.x, c.y + r * 0.22f), col, t);
    dl->AddCircleFilled(ImVec2(c.x, c.y + r * 0.50f), ui::metrics::stroke(1.4f), col);
}

// Disclosure chevron: a triangle pointing +x at ang 0 and down at ang PI/2, so the SAME shape
// carries the open state continuously instead of snapping between two ImGui arrow directions.
void drawChevron(ImDrawList* dl, ImVec2 c, float r, float ang, ImU32 col) {
    const float ca = std::cos(ang), sa = std::sin(ang);
    auto rot = [&](float x, float y) {
        return ImVec2(c.x + x * ca - y * sa, c.y + x * sa + y * ca);
    };
    dl->AddTriangleFilled(rot(r * 0.85f, 0.0f), rot(-r * 0.55f, -r * 0.72f),
                          rot(-r * 0.55f, r * 0.72f), col);
}

// Aspect-agnostic thumbnail: a rounded matte plate plus the texture inside it. ImTextureID carries
// no dimensions, so the plate (not a letterbox computation) is what keeps a non-square or
// transparent preview reading as a framed tile rather than a hole.
void drawThumb(ImDrawList* dl, ImTextureID tex, ImVec2 a, ImVec2 b) {
    if (b.x <= a.x || b.y <= a.y) return;
    const float rnd = chipRound();
    dl->AddRectFilled(a, b, roleU32(palette.codeBg, kMatteA), rnd);
    const float inset = ui::metrics::stroke(1.0f);
    dl->AddImage(tex, ImVec2(a.x + inset, a.y + inset), ImVec2(b.x - inset, b.y - inset));
}

// ---- shared chip geometry -----------------------------------------------------------------------
struct ChipGeom {
    float  w = 0.0f, h = 0.0f, padX = 0.0f, padY = 0.0f, gap = 0.0f, dotR = 0.0f;
    ImVec2 textSize = ImVec2(0.0f, 0.0f);
};

ChipGeom chipGeom(const char* label, bool withDot) {
    ChipGeom g;
    g.padX     = ui::metrics::U(1.5f);
    g.padY     = ui::metrics::U(0.5f);
    g.gap      = ui::metrics::U(1.0f);
    g.dotR     = withDot ? ui::metrics::U(0.75f) : 0.0f;
    g.textSize = ImGui::CalcTextSize(label ? label : "", nullptr, true);   // "##id" suffix hidden
    g.h        = g.textSize.y + g.padY * 2.0f;
    g.w        = g.padX * 2.0f + g.textSize.x + (withDot ? (g.dotR * 2.0f + g.gap) : 0.0f);
    return g;
}

// ---- buttons --------------------------------------------------------------------------------
// Shared body for primary/secondary/danger. A disabled button still RENDERS and still answers
// "why can't I click this?" on hover — the kit never hides an action the user is hunting. It
// gets ONE explicit disabled look for every kind: a neutral frame fill, a hairline border and dim
// (still legible) text. No BeginDisabled alpha: that scaled the accent fill to a washed-out pale
// orange with near-white text that read as neither enabled nor disabled.
bool kitButton(const char* label, bool enabled, const char* disabledReason, const ImVec2& size,
               const ImVec4& base, const ImVec4& hovered, const ImVec4& active,
               const ImVec4& text) {
    ImVec2 sz = size;
    if (sz.y <= 0.0f) sz.y = ImGui::GetFrameHeight();

    int vars = 1, cols = 4;
    if (enabled) {
        ImGui::PushStyleColor(ImGuiCol_Button,        base);
        ImGui::PushStyleColor(ImGuiCol_ButtonHovered, hovered);
        ImGui::PushStyleColor(ImGuiCol_ButtonActive,  active);
        ImGui::PushStyleColor(ImGuiCol_Text,          text);
    } else {
        const ImVec4 fill = roleVec(palette.frameBg, kBtnDisabledA);
        ImGui::PushStyleColor(ImGuiCol_Button,        fill);
        ImGui::PushStyleColor(ImGuiCol_ButtonHovered, fill);
        ImGui::PushStyleColor(ImGuiCol_ButtonActive,  fill);
        ImGui::PushStyleColor(ImGuiCol_Text,          roleVec(palette.textDim));
        ImGui::PushStyleColor(ImGuiCol_Border,        roleVec(palette.border));
        ImGui::PushStyleVar(ImGuiStyleVar_FrameBorderSize, ui::metrics::stroke(1.0f));
        ++cols;
        ++vars;
    }
    ImGui::PushStyleVar(ImGuiStyleVar_FrameRounding, chipRound());

    // The disabled ITEM flag (not BeginDisabled) blocks presses without the global alpha.
    if (!enabled) ImGui::PushItemFlag(ImGuiItemFlags_Disabled, true);
    bool pressed = ImGui::Button(label ? label : "", sz);
    if (!enabled) {
        ImGui::PopItemFlag();
        pressed = false;
        // Disabled items do not report hover normally; AllowWhenDisabled is what makes the
        // explanation reachable at all.
        if (ImGui::IsItemHovered(ImGuiHoveredFlags_AllowWhenDisabled)) kitTooltip(disabledReason);
    }

    ImGui::PopStyleVar(vars);
    ImGui::PopStyleColor(cols);
    return pressed;
}

// ---- centred text (empty / blocked states) ------------------------------------------------------
// Every line is centred under the (centred) title. A line that does not fit WRAPS at word
// boundaries inside a small inset and each wrapped line is centred on its own, so a paragraph
// never reads as a ragged left-aligned block under a centred heading — and never centres to a
// negative offset that clips it at both ends. Explicit '\n' starts a new line.
void centeredOrWrapped(const char* text, const ImVec4& col) {
    if (!text || !text[0]) return;
    const float avail  = ImGui::GetContentRegionAvail().x;
    const float startX = ImGui::GetCursorPosX();
    const float inset  = ImGui::CalcTextSize(text).x <= avail ? 0.0f : avail * kWrapInsetFrac;
    const float wrapW  = ImMax(avail - inset * 2.0f, ImGui::GetFontSize());
    ImFont*     font   = ImGui::GetFont();
    const float scale  = ImGui::GetFontSize() / ImMax(font->FontSize, 1.0f);
    const char* end    = text + std::strlen(text);

    ImGui::PushStyleColor(ImGuiCol_Text, col);
    for (const char* para = text; para <= end;) {
        const char* nl = static_cast<const char*>(std::memchr(para, '\n', (size_t)(end - para)));
        const char* pe = nl ? nl : end;
        const char* s  = para;
        do {
            const char* e = s < pe ? font->CalcWordWrapPositionA(scale, s, pe, wrapW) : pe;
            if (e <= s && s < pe) {   // narrower than one glyph: take one UTF-8 character
                unsigned int c = 0;
                e = s + ImMax(1, ImTextCharFromUtf8(&c, s, pe));
            }
            const char* le = e;
            while (le > s && (le[-1] == ' ' || le[-1] == '\t')) --le;
            const float lw = ImGui::CalcTextSize(s, le).x;
            ImGui::SetCursorPosX(startX + ImMax(0.0f, (avail - lw) * 0.5f));
            ImGui::TextUnformatted(s, le);
            s = e;
            while (s < pe && (*s == ' ' || *s == '\t')) ++s;
        } while (s < pe);
        if (!nl) break;
        para = nl + 1;
    }
    ImGui::PopStyleColor();
}

// Centred action button shared by emptyState/blockedState.
bool centeredAction(const char* label, bool enabled) {
    if (!label || !label[0]) return false;
    const float rowLeft = ImGui::GetCursorScreenPos().x;
    const float availW  = ImGui::GetContentRegionAvail().x;
    const float bw      = ImMin(availW, ImGui::CalcTextSize(label).x + padX() * 4.0f);
    const ImVec2 c      = ImGui::GetCursorScreenPos();
    ImGui::SetCursorScreenPos(ImVec2(rowLeft + ImMax(0.0f, (availW - bw) * 0.5f), c.y));
    return primaryButton(label, enabled, nullptr, ImVec2(bw, ImGui::GetFrameHeight()));
}

// ---- density stack -------------------------------------------------------------------------------
std::vector<Density>& densityStack() {
    static std::vector<Density> s{Density::Cozy};
    return s;
}

// ---- card nesting ---------------------------------------------------------------------------------
// One entry per live cardBegin(). cardEnd() is a hard no-op on an empty stack, so an unmatched End
// can never pop ImGui state it did not push.
struct CardFrame {
    int           styleVars    = 0;
    int           styleColors  = 0;
    float         contentLeft  = 0.0f;
    float         contentRight = 0.0f;
    // Screen x just past the header's chevron/icon/title. cardHeaderRight() right-aligns against
    // contentRight but must never rewind PAST this, or a wide chip lands on top of the title.
    float         headerEndX   = 0.0f;
    float         headerBlockH = 0.0f;
    float         padYUsed     = 0.0f;
    float         hover        = 0.0f;
    bool          hasHeader    = false;
    bool          open         = true;
    bool          animating    = false;
    bool          hasTint      = false;
    float         tint[3]      = {0.0f, 0.0f, 0.0f};
    ImGuiStorage* store        = nullptr;   // PARENT window's storage (survives the child)
    ImGuiID       bodyKey      = 0;
};
std::vector<CardFrame> g_cards;

// ---- row nesting ------------------------------------------------------------------------------------
struct RowFrame {
    ImVec2 origin = ImVec2(0.0f, 0.0f);
    ImVec2 after  = ImVec2(0.0f, 0.0f);   // cursor ImGui left after the row's hit item
};
std::vector<RowFrame> g_rows;

// The one row treatment: frosted pill, animated hover wash, animated selection glow. Called after
// the hit item is registered so the pill lands under the row's own content.
void paintRow(ImVec2 p0, ImVec2 p1, bool hovered, bool selected) {
    ImDrawList* dl  = ImGui::GetWindowDrawList();
    const float rDp = dpOf(rowRound());
    const float hv  = anim::toward(ImGui::GetID("##kit_row_hv"), hovered ? 1.0f : 0.0f, anim::kFast);
    const float sl  = anim::toward(ImGui::GetID("##kit_row_sel"), selected ? 1.0f : 0.0f, anim::kFast);
    ui::glassRowBackground(dl, p0, p1, rDp, false, false, kRowBaseA + hv * kRowHoverA);
    if (sl > kSettled) {
        float acc[4];
        Palette::withAlpha(palette.accent, acc, sl);
        ui::glassSelectionGlow(dl, p0, p1, rDp, acc);
    }
}

}  // namespace

// ==================================================================================================
// semantic tone
// ==================================================================================================
ImU32  toneU32(Tone t, float alphaMul) { return roleU32(toneRole(t), alphaMul); }
ImVec4 toneVec(Tone t, float alphaMul) { return roleVec(toneRole(t), alphaMul); }

// Semantic chroma retuned for the surface it lands on. The palette roles are picked against the
// theme's OWN panel background; on a frosted panel the tone underneath is whatever the viewport
// shows. Keep the hue, move the weight toward the surface's text tone: bright roles darken over a
// bright frost, dark roles lift over a dark one. (Same math as the copilot's semanticOnGlass,
// which is TU-private there.)
ImVec4 toneOn(Tone t, const AdaptiveSurface& surf) {
    const float* role = toneRole(t);
    ImVec4       c(role[0], role[1], role[2], 1.0f);
    const float  lum = 0.2126f * c.x + 0.7152f * c.y + 0.0722f * c.z;
    if (surf.darkText && lum > 0.45f) {          // bright hue on a bright frost -> deepen
        c.x *= 0.52f;
        c.y *= 0.52f;
        c.z *= 0.52f;
    } else if (!surf.darkText && lum < 0.35f) {  // dark hue on a dark frost -> lift
        c.x += (1.0f - c.x) * 0.55f;
        c.y += (1.0f - c.y) * 0.55f;
        c.z += (1.0f - c.z) * 0.55f;
    }
    return c;
}

// ==================================================================================================
// density
// ==================================================================================================
void    pushDensity(Density d) { densityStack().push_back(d); }
void    popDensity()           { auto& s = densityStack(); if (s.size() > 1) s.pop_back(); }
Density density()              { return densityStack().back(); }

float padX() {
    switch (density()) {
        case Density::Compact: return ui::metrics::cpChatCardPadX();
        case Density::Roomy:   return ui::metrics::reCardPadX();
        case Density::Cozy:
        default:               return ui::metrics::cpMenuRowPadX();
    }
}
float padY() {
    switch (density()) {
        case Density::Compact: return ui::metrics::cpChatCardPadY();
        case Density::Roomy:   return ui::metrics::reCardPadY();
        case Density::Cozy:
        default:               return ui::metrics::cpMenuRowPadY();
    }
}
float rowGap() {
    switch (density()) {
        case Density::Compact: return ui::metrics::cpChatRowGap();
        case Density::Roomy:   return ui::metrics::reCardGap();
        case Density::Cozy:
        default:               return ui::metrics::cpMenuRowGap();
    }
}
float rowRound() {
    switch (density()) {
        case Density::Roomy: return ui::metrics::reCardRound();
        case Density::Compact:
        case Density::Cozy:
        default:             return ui::metrics::cpMenuRowRound();
    }
}
float chipRound() {
    switch (density()) {
        case Density::Roomy: return ui::metrics::reChipRound();
        case Density::Compact:
        case Density::Cozy:
        default:             return ui::metrics::cpMenuChipRound();
    }
}
float sectionGap() {
    switch (density()) {
        // A streaming conversation cannot afford the docked pane's full section rhythm: a run of
        // sections would otherwise spend more height on gaps than on content.
        case Density::Compact: return ui::metrics::cpMenuSectionGap() * 0.6f;
        case Density::Roomy:   return ui::metrics::reSectionGap();
        case Density::Cozy:
        default:               return ui::metrics::cpMenuSectionGap();
    }
}
float headGap() {
    switch (density()) {
        case Density::Roomy: return ui::metrics::reHeadGap();
        case Density::Compact:
        case Density::Cozy:
        default:             return ui::metrics::cpMenuHeadGap();
    }
}
float gutter() {
    switch (density()) {
        case Density::Roomy: return ui::metrics::reGutter();
        case Density::Compact:
        case Density::Cozy:
        default:             return ui::metrics::cpMenuGutter();
    }
}

// ==================================================================================================
// cards
// ==================================================================================================
bool cardBegin(const char* id, const char* title, const char* icon, bool* open,
               const float tint[3]) {
    ImGuiContext& g = *GImGui;
    ImGui::PushID(id && id[0] ? id : "kit_card");

    const bool canCollapse = (open != nullptr);
    const bool isOpen      = canCollapse ? *open : true;

    // Resolve the disclosure tween and the remembered body height BEFORE BeginChild: both keys
    // belong to the PARENT window and must survive the child's own id-stack reset.
    ImGuiStorage* store   = ImGui::GetStateStorage();
    const ImGuiID bodyKey = ImGui::GetID("##kit_card_bodyh");
    const ImGuiID animId  = ImGui::GetID("##kit_card_open");
    const float   t       = canCollapse ? anim::toward(animId, isOpen ? 1.0f : 0.0f, anim::kBase)
                                        : 1.0f;
    const float   bodyH   = store->GetFloat(bodyKey, 0.0f);
    const bool    animating = canCollapse && t > kSettled && t < 1.0f - kSettled;

    const float txtH       = ImGui::GetTextLineHeight();
    const float iconSz     = ui::metrics::iconHeader();
    const bool  wantHeader = (title && title[0]) || (icon && icon[0]) || canCollapse;
    const float rowH       = wantHeader ? ((icon && icon[0]) ? ImMax(txtH, iconSz) : txtH) : 0.0f;
    const float gapH       = wantHeader ? headGap() * t : 0.0f;
    const float headerH    = rowH + gapH;
    const float pY         = padY();

    // Open: auto-resize to the body. Mid-collapse: a FIXED height that shrinks through the
    // remembered body height, so the body clips away instead of vanishing on one frame.
    float           childH = 0.0f;
    ImGuiChildFlags cf     = ImGuiChildFlags_Borders;
    if (animating)                    childH = pY * 2.0f + headerH + bodyH * t;
    else if (canCollapse && !isOpen)  childH = pY * 2.0f + headerH;
    else                              cf |= ImGuiChildFlags_AutoResizeY;

    ImGui::PushStyleColor(ImGuiCol_ChildBg, roleVec(palette.glassBg, 0.0f));  // we paint our own
    ImGui::PushStyleColor(ImGuiCol_Border,  roleVec(palette.glassBorder));
    ImGui::PushStyleVar(ImGuiStyleVar_ChildRounding,   rowRound());
    ImGui::PushStyleVar(ImGuiStyleVar_ChildBorderSize, ui::metrics::stroke(1.0f));
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding,   ImVec2(padX(), pY));
    ImGui::BeginChild("kit_card_body", ImVec2(0.0f, childH), cf,
                      ImGuiWindowFlags_NoScrollbar | ImGuiWindowFlags_NoScrollWithMouse);
    // A brand-new auto-sized card has never measured its content, so on its first frame the child
    // is only its padding tall: border + wash would paint an EMPTY rounded shell with the title
    // clipped away. Lay it out (sizes measure) but do not render it that one frame, and pump the
    // next frame so it appears at its real height at once. Never hides a card seen before.
    if (cf & ImGuiChildFlags_AutoResizeY) {
        ImGuiWindow* cw = ImGui::GetCurrentWindow();
        if (cw->Appearing && cw->ContentSize.y <= 0.0f) {
            cw->Hidden = true;
            anim::requestFrames();
        }
    }
    // Content draws on channel 1; cardEnd paints the wash behind it on channel 0 once the height
    // is final. Split + merge are unconditional and paired.
    ImGui::GetWindowDrawList()->ChannelsSplit(2);
    ImGui::GetWindowDrawList()->ChannelsSetCurrent(1);

    CardFrame f;
    f.styleVars    = 3;
    f.styleColors  = 2;
    f.contentLeft  = ImGui::GetCursorScreenPos().x;
    f.contentRight = f.contentLeft + ImGui::GetContentRegionAvail().x;
    f.headerBlockH = headerH;
    f.padYUsed     = pY;
    f.open         = isOpen;
    f.animating    = animating;
    f.store        = store;
    f.bodyKey      = bodyKey;
    if (tint) {
        f.hasTint = true;
        f.tint[0] = tint[0];
        f.tint[1] = tint[1];
        f.tint[2] = tint[2];
    }
    g_cards.push_back(f);

    const bool bodyVisible = !canCollapse || t > kSettled;
    if (!wantHeader) return bodyVisible;

    ImDrawList*  dl     = ImGui::GetWindowDrawList();
    const float  rowW   = ImMax(f.contentRight - f.contentLeft, ui::metrics::stroke(1.0f));
    const ImVec2 rowMin = ImGui::GetCursorScreenPos();

    // The header row is ONE item (its height already carries the head gap) so cardHeaderRight's
    // SameLine rewind lands on the title row rather than on a spacer.
    bool toggled = false;
    if (canCollapse) toggled = ImGui::InvisibleButton("kit_card_head", ImVec2(rowW, rowH + gapH));
    else             ImGui::Dummy(ImVec2(rowW, rowH + gapH));
    if (canCollapse)
        IMGUI_TEST_ENGINE_ITEM_INFO(
            ImGui::GetItemID(), title ? title : "",
            GImGui->LastItemData.StatusFlags | ImGuiItemStatusFlags_Openable |
                (isOpen ? ImGuiItemStatusFlags_Opened : 0));
    const float hv = anim::toward(ImGui::GetID("##kit_card_hv"),
                                  ImGui::IsItemHovered() ? 1.0f : 0.0f, anim::kFast);
    g_cards.back().hover = hv;

    float       x    = rowMin.x;
    const float cy   = rowMin.y + rowH * 0.5f;
    const float gap  = ui::metrics::U(1.5f);
    if (canCollapse) {
        drawChevron(dl, ImVec2(x + txtH * 0.5f, cy), txtH * 0.30f, t * IM_PI * 0.5f,
                    roleU32(palette.textDim, kChipIdleTextA + (1.0f - kChipIdleTextA) * hv));
        x += txtH + gap;
    }
    if (icon && icon[0] &&
        drawIconAt(dl, icon, ImVec2(x + iconSz * 0.5f, cy), iconSz, roleU32(palette.accent))) {
        x += iconSz + gap;
    }
    if (title && title[0]) {
        const std::string lab = elide(title, ImMax(f.contentRight - x, ui::metrics::stroke(1.0f)));
        dl->AddText(ImVec2(x, rowMin.y + (rowH - txtH) * 0.5f), roleU32(palette.text), lab.c_str());
        x += ImGui::CalcTextSize(lab.c_str()).x;
    }
    g_cards.back().headerEndX = x + gap;
    g_cards.back().hasHeader  = true;

    if (toggled && open) {
        *open = !isOpen;
        g_cards.back().open = *open;
    }
    return bodyVisible;
}

void cardEnd() {
    if (g_cards.empty()) return;   // unmatched End: refuse rather than corrupt the ImGui stacks
    const CardFrame f = g_cards.back();
    g_cards.pop_back();

    ImDrawList*  dl = ImGui::GetWindowDrawList();
    const ImVec2 mn = ImGui::GetWindowPos();
    const ImVec2 sz = ImGui::GetWindowSize();
    const ImVec2 mx(mn.x + sz.x, mn.y + sz.y);

    dl->ChannelsSetCurrent(0);
    const float accentTint[3] = {palette.accent[0], palette.accent[1], palette.accent[2]};
    ui::glassCardWash(dl, mn, mx, dpOf(rowRound()), f.hasTint ? f.tint : accentTint,
                      kCardWashA + f.hover * kCardHoverA);
    dl->ChannelsMerge();

    // Remember the settled body height so the NEXT collapse has a real distance to shrink
    // through. Only a fully open, non-animating card measures a truthful body.
    if (f.store && f.open && !f.animating) {
        const float body = sz.y - (f.padYUsed * 2.0f + f.headerBlockH);
        f.store->SetFloat(f.bodyKey, body > 0.0f ? body : 0.0f);
    }

    ImGui::EndChild();
    ImGui::PopStyleVar(f.styleVars);
    ImGui::PopStyleColor(f.styleColors);
    ImGui::PopID();
    // No trailing gap here on purpose — gapRow() is the explicit between-cards rhythm token.
}

void cardHeaderRight(float widgetsWidth) {
    if (g_cards.empty()) return;
    const CardFrame& f = g_cards.back();
    float            x = f.contentRight - widgetsWidth;
    // Floor at the end of the TITLE, not at the content edge: clamping to contentLeft when the
    // widget is wider than the free space parks the chip on top of the title. Overflowing right
    // (and being clipped by the card) is strictly better than overlap.
    const float floorX = f.headerEndX > 0.0f ? f.headerEndX : f.contentLeft;
    if (x < floorX) x = floorX;
    if (f.hasHeader) ImGui::SameLine(0.0f, 0.0f);
    ImGui::SetCursorScreenPos(ImVec2(x, ImGui::GetCursorScreenPos().y));
}

// ==================================================================================================
// section header
// ==================================================================================================
void sectionHeader(const char* label, const char* icon, const char* trailing, int count,
                   bool first) {
    if (!first) ImGui::Dummy(ImVec2(0.0f, sectionGap()));

    ImDrawList*  dl     = ImGui::GetWindowDrawList();
    const float  txtH   = ImGui::GetTextLineHeight();
    const ImVec2 rowMin = ImGui::GetCursorScreenPos();
    const float  rowW   = ImMax(ImGui::GetContentRegionAvail().x, ui::metrics::stroke(1.0f));
    const float  gap    = ui::metrics::U(1.5f);

    float x = rowMin.x;
    if (icon && icon[0] &&
        drawIconAt(dl, icon, ImVec2(x + txtH * 0.5f, rowMin.y + txtH * 0.5f), txtH,
                   roleU32(palette.accent))) {
        x += txtH + gap;
    }

    // Accent label, and deliberately NO underline rule: the PLM hub removed it because it read as
    // clutter beside the boxed glass panels, and that decision is now the app-wide one.
    const std::string lab = elide(label, ImMax(rowMin.x + rowW - x, ui::metrics::stroke(1.0f)));
    dl->AddText(ImVec2(x, rowMin.y), roleU32(palette.accent), lab.c_str());
    x += ImGui::CalcTextSize(lab.c_str()).x;

    if (count >= 0) {
        const std::string c = fmtCount((long long)count);
        x += gap;
        dl->AddText(ImVec2(x, rowMin.y), roleU32(palette.textDim), c.c_str());
        x += ImGui::CalcTextSize(c.c_str()).x;
    }
    if (trailing && trailing[0]) {
        // Right-aligned secondary text on the header's own line, so a pane stops spending a whole
        // dim line under the header on a number. Dropped rather than overlapped when it cannot fit.
        const ImVec2 ts = ImGui::CalcTextSize(trailing);
        const float  tx = rowMin.x + rowW - ts.x;
        if (tx > x + gap) dl->AddText(ImVec2(tx, rowMin.y), roleU32(palette.textDim), trailing);
    }

    ImGui::Dummy(ImVec2(rowW, txtH));
    ImGui::Dummy(ImVec2(0.0f, headGap()));
}

// ==================================================================================================
// rows
// ==================================================================================================
bool row(const char* id, const char* title, const RowStyle& style) {
    ImGui::PushID(id && id[0] ? id : "kit_row");

    const float lineH     = ImGui::GetTextLineHeight();
    const bool  hasSecond = style.secondary && style.secondary[0];
    const float lineGap   = hasSecond ? ui::metrics::U(0.5f) : 0.0f;
    const float contentH  = lineH + (hasSecond ? lineGap + lineH : 0.0f);
    const float pX        = padX();
    const float pY        = padY();
    const float h         = contentH + pY * 2.0f;
    const float w         = ImMax(ImGui::GetContentRegionAvail().x, ui::metrics::stroke(1.0f));

    const ImVec2 p0 = ImGui::GetCursorScreenPos();
    const ImVec2 p1(p0.x + w, p0.y + h);

    bool clicked = false, hovered = false;
    if (style.disabled) {
        ImGui::Dummy(ImVec2(w, h));
    } else {
        clicked = ImGui::InvisibleButton("##kit_rowhit", ImVec2(w, h));
        hovered = ImGui::IsItemHovered();
    }
    paintRow(p0, p1, hovered, style.selected);

    ImDrawList* dl     = ImGui::GetWindowDrawList();
    const float dimMul = style.disabled ? kDisabledA : 1.0f;
    const float gap    = ui::metrics::U(1.5f);

    // Leading slot: thumbnail, else a named icon, else nothing. No generic dot bullet — that
    // placeholder is one of the things this kit exists to delete.
    float x = p0.x + pX;
    if (style.thumb) {
        drawThumb(dl, style.thumb, ImVec2(x, p0.y + pY), ImVec2(x + contentH, p0.y + pY + contentH));
        x += contentH + gap;
    } else if (style.icon && style.icon[0]) {
        const float glyph = ImMin(contentH, ui::metrics::iconHeader());
        if (drawIconAt(dl, style.icon, ImVec2(x + contentH * 0.5f, p0.y + h * 0.5f), glyph,
                       toneU32(style.iconTone, dimMul))) {
            x += contentH + gap;
        }
    }

    // Reserve the caller's action lane, then the meta column, and give the rest to the text.
    const float rightEdge = p1.x - pX - ImMax(style.actionsW, 0.0f);
    float       textRight = rightEdge;
    if (style.meta && style.meta[0]) {
        const ImVec2 ms  = ImGui::CalcTextSize(style.meta);
        const float  mx0 = rightEdge - ms.x;
        if (mx0 > x + gap) {
            dl->AddText(ImVec2(mx0, p0.y + (h - ms.y) * 0.5f),
                        roleU32(palette.textDim, dimMul), style.meta);
            textRight = mx0 - gap;
        }
    }

    const float       textW = ImMax(textRight - x, ui::metrics::stroke(1.0f));
    const std::string t1    = elide(title, textW);
    dl->AddText(ImVec2(x, p0.y + pY),
                roleU32(style.disabled ? palette.textDisabled : palette.text), t1.c_str());
    if (hasSecond) {
        const std::string t2 = elide(style.secondary, textW);
        dl->AddText(ImVec2(x, p0.y + pY + lineH + lineGap), roleU32(palette.textDim, dimMul),
                    t2.c_str());
    }

    if (hovered) {
        // An elided title is never left ambiguous: the caller's tooltip wins, the full title is
        // the fallback.
        if (style.tooltip && style.tooltip[0])            kitTooltip(style.tooltip);
        else if (title && title[0] && t1 != title)        kitTooltip(title);
    }

    ImGui::PopID();
    return clicked;
}

bool rowBegin(const char* id, float height, bool selected) {
    ImGui::PushID(id && id[0] ? id : "kit_row");

    const float w = ImMax(ImGui::GetContentRegionAvail().x, ui::metrics::stroke(1.0f));
    const float h = height > 0.0f ? height : ImGui::GetTextLineHeight() + padY() * 2.0f;

    const ImVec2 p0      = ImGui::GetCursorScreenPos();
    const bool   clicked = ImGui::InvisibleButton("##kit_rowhit", ImVec2(w, h));
    const bool   hovered = ImGui::IsItemHovered();
    const ImVec2 after   = ImGui::GetCursorScreenPos();

    paintRow(p0, ImVec2(p0.x + w, p0.y + h), hovered, selected);

    RowFrame f;
    f.origin = p0;
    f.after  = after;
    g_rows.push_back(f);

    ImGui::SetCursorScreenPos(ImVec2(p0.x + padX(), p0.y + padY()));
    return clicked;
}

void rowEnd() {
    if (g_rows.empty()) return;   // unmatched End: refuse rather than corrupt the ImGui stacks
    ImGui::SetCursorScreenPos(g_rows.back().after);
    g_rows.pop_back();
    ImGui::PopID();
}

// ==================================================================================================
// chips / badges
// ==================================================================================================
void chip(const char* label, Tone t, bool dot, const char* tooltip) {
    const char*    lab = label ? label : "";
    const ChipGeom g   = chipGeom(lab, dot);
    const ImVec2   p0  = ImGui::GetCursorScreenPos();
    ImGui::Dummy(ImVec2(g.w, g.h));
    const bool hovered = ImGui::IsItemHovered();

    ImDrawList*  dl = ImGui::GetWindowDrawList();
    const ImVec2 p1(p0.x + g.w, p0.y + g.h);
    const float* role = toneRole(t);
    const float  rnd  = chipRound();
    const float  hv   = anim::toward(ImGui::GetID(lab), hovered ? 1.0f : 0.0f, anim::kFast);

    dl->AddRectFilled(p0, p1, roleU32(role, kChipFillA), rnd);
    dl->AddRect(p0, p1, roleU32(role, kChipBorderA + (1.0f - kChipBorderA) * hv), rnd, 0,
                ui::metrics::stroke(1.0f));
    if (dot) dl->AddCircleFilled(ImVec2(p0.x + g.padX + g.dotR, p0.y + g.h * 0.5f), g.dotR,
                                 roleU32(role));
    dl->AddText(ImVec2(p0.x + g.padX + (dot ? g.dotR * 2.0f + g.gap : 0.0f), p0.y + g.padY),
                roleU32(role), lab, ImGui::FindRenderedTextEnd(lab));
    if (hovered) kitTooltip(tooltip);
}

void valueChip(const char* label, const char* value, Tone t, const char* tooltip) {
    const char* lab  = label ? label : "";
    const char* val  = value ? value : "";
    const float pX   = ui::metrics::U(1.5f);
    const float pY   = ui::metrics::U(0.5f);
    const float gap  = ui::metrics::U(1.0f);
    const ImVec2 ls  = ImGui::CalcTextSize(lab);
    const ImVec2 vs  = ImGui::CalcTextSize(val);
    const float  h   = ImMax(ls.y, vs.y) + pY * 2.0f;
    const float  w   = pX * 2.0f + ls.x + gap + vs.x;

    const ImVec2 p0 = ImGui::GetCursorScreenPos();
    ImGui::Dummy(ImVec2(w, h));
    const bool hovered = ImGui::IsItemHovered();

    ImDrawList*  dl = ImGui::GetWindowDrawList();
    const ImVec2 p1(p0.x + w, p0.y + h);
    const float* role = toneRole(t);
    const float  rnd  = chipRound();
    dl->AddRectFilled(p0, p1, roleU32(role, kChipFillA), rnd);
    dl->AddRect(p0, p1, roleU32(role, kChipBorderA), rnd, 0, ui::metrics::stroke(1.0f));
    dl->AddText(ImVec2(p0.x + pX, p0.y + (h - ls.y) * 0.5f), roleU32(palette.textDim), lab);
    dl->AddText(ImVec2(p0.x + pX + ls.x + gap, p0.y + (h - vs.y) * 0.5f), roleU32(role), val);
    if (hovered) kitTooltip(tooltip);
}

bool toggleChip(const char* label, bool* on, Tone t, const char* tooltip) {
    const char* lab = label ? label : "";
    ImGui::PushID(lab);

    const ChipGeom g  = chipGeom(lab, /*withDot=*/false);
    const ImVec2   p0 = ImGui::GetCursorScreenPos();
    const bool clicked = ImGui::InvisibleButton("##kit_toggle", ImVec2(g.w, g.h));
    const bool hovered = ImGui::IsItemHovered();
    if (clicked && on) *on = !*on;
    {
        ImGuiContext& g = *GImGui;
        IMGUI_TEST_ENGINE_ITEM_INFO(
            ImGui::GetItemID(), lab, g.LastItemData.StatusFlags |
            ImGuiItemStatusFlags_Checkable | (on && *on ? ImGuiItemStatusFlags_Checked : 0));
    }

    const bool  isOn = on && *on;
    const float fill = anim::toward(ImGui::GetID("##fill"), isOn ? 1.0f : 0.0f, anim::kFast);
    const float hv   = anim::toward(ImGui::GetID("##hv"), hovered ? 1.0f : 0.0f, anim::kFast);

    ImDrawList*  dl = ImGui::GetWindowDrawList();
    const ImVec2 p1(p0.x + g.w, p0.y + g.h);
    const float* role = toneRole(t);
    const float  rnd  = chipRound();
    dl->AddRectFilled(p0, p1, roleU32(role, kChipFillA * fill + kSegHoverA * hv * (1.0f - fill)),
                      rnd);
    // Border strengthens with the fill, and lifts halfway on hover so an OFF chip still answers
    // the cursor.
    dl->AddRect(p0, p1,
                roleU32(role, kChipOffBorderA +
                                  (1.0f - kChipOffBorderA) * ImMax(fill, hv * 0.5f)),
                rnd, 0, ui::metrics::stroke(1.0f));
    dl->AddText(ImVec2(p0.x + g.padX, p0.y + g.padY),
                u32(lerpVec4(roleVec(palette.textDim), roleVec(role), fill)), lab,
                ImGui::FindRenderedTextEnd(lab));
    if (hovered) kitTooltip(tooltip);

    ImGui::PopID();
    return clicked;
}

// ==================================================================================================
// buttons
// ==================================================================================================
bool primaryButton(const char* label, bool enabled, const char* disabledReason,
                   const ImVec2& size) {
    const ImVec4 base = roleVec(palette.accent, kPrimaryFillA);
    return kitButton(label, enabled, disabledReason, size, base, roleVec(palette.accent),
                     roleVec(palette.accentDim), ui::maxContrastTextOn(base));
}

bool secondaryButton(const char* label, bool enabled, const char* disabledReason,
                     const ImVec2& size) {
    return kitButton(label, enabled, disabledReason, size, roleVec(palette.button),
                     roleVec(palette.buttonHovered), roleVec(palette.buttonActive),
                     roleVec(palette.text));
}

bool dangerButton(const char* label, bool enabled, const char* disabledReason,
                  const ImVec2& size) {
    const ImVec4 base = roleVec(palette.statusErr, kDangerFillA);
    return kitButton(label, enabled, disabledReason, size, base, roleVec(palette.statusErr),
                     roleVec(palette.statusErr, kDangerActA), ui::maxContrastTextOn(base));
}

bool iconButton(const char* id, const char* icon, const char* tooltip, bool enabled, Tone t,
                float size) {
    ImGui::PushID(id && id[0] ? id : "kit_iconbtn");

    const float  s  = size > 0.0f ? size : ImGui::GetFrameHeight();
    const ImVec2 p0 = ImGui::GetCursorScreenPos();

    if (!enabled) ImGui::BeginDisabled();
    bool clicked = ImGui::InvisibleButton("##kit_iconhit", ImVec2(s, s));
    if (!enabled) {
        ImGui::EndDisabled();
        clicked = false;
    }
    const bool hovered = ImGui::IsItemHovered(ImGuiHoveredFlags_AllowWhenDisabled);

    const float hv = anim::toward(ImGui::GetID("##hv"), (hovered && enabled) ? 1.0f : 0.0f,
                                  anim::kFast);
    ImDrawList*  dl = ImGui::GetWindowDrawList();
    const ImVec2 p1(p0.x + s, p0.y + s);
    if (hv > kSettled) {
        dl->AddRectFilled(p0, p1, roleU32(palette.text, kBtnGhostHoverA * hv), chipRound());
    }
    // Glyph per icon tier: at least kIconFillFrac of the button (a frame-height button lands on
    // the header tier, ~iconHeader), and a text-height button still shows a full font-size glyph.
    const float glyph = ImMax(s * kIconFillFrac, ImMin(s, ImGui::GetFontSize()));
    drawIconAt(dl, icon, ImVec2(p0.x + s * 0.5f, p0.y + s * 0.5f), glyph,
               enabled ? toneU32(t) : roleU32(palette.textDisabled));

    // An unlabelled glyph with no tooltip is not a discoverable control, so this is unconditional.
    if (hovered) kitTooltip(tooltip);

    ImGui::PopID();
    return clicked;
}

bool segmented(const char* id, const char* const* labels, int count, int* selected, float width,
               const char* const* tooltips) {
    if (!labels || count <= 0 || !selected) return false;
    [[maybe_unused]] ImGuiContext& g = *GImGui;

    ImGui::PushID(id && id[0] ? id : "kit_seg");

    const float h   = ImGui::GetFrameHeight();
    const float pX  = padX();
    // Segments are equal width, so the width that matters is the WIDEST label's — not the
    // average. Summing every label and then dividing by count (what this did) gives each segment
    // the mean width, so any set with one long and two short labels elides the long one:
    // {"Links","All","Assembly"} rendered as "Asse...". An explicit `width` still wins, and is
    // still divided evenly, because a caller passing one is fitting a known column.
    float segW = 0.0f;
    if (width > 0.0f) {
        segW = width / (float)count;
    } else {
        for (int i = 0; i < count; ++i)
            segW = ImMax(segW, ImGui::CalcTextSize(labels[i] ? labels[i] : "").x);
        segW += pX * 2.0f;
    }
    segW = ImMax(segW, ui::metrics::stroke(1.0f));
    const float w = segW * (float)count;

    const int   sel = ImClamp(*selected, 0, count - 1);
    const ImVec2 p0 = ImGui::GetCursorScreenPos();
    const ImVec2 p1(p0.x + w, p0.y + h);

    ImDrawList* dl  = ImGui::GetWindowDrawList();
    const float rnd = chipRound();
    dl->AddRectFilled(p0, p1, roleU32(palette.frameBg, kTrackA), rnd);
    dl->AddRect(p0, p1, roleU32(palette.border), rnd, 0, ui::metrics::stroke(1.0f));

    // The selection SLIDES between segments rather than teleporting, so the eye keeps track of
    // which one it left.
    const float selT  = anim::toward(ImGui::GetID("##sel"), (float)sel, anim::kBase);
    const float inset = ui::metrics::stroke(2.0f);
    dl->AddRectFilled(ImVec2(p0.x + segW * selT + inset, p0.y + inset),
                      ImVec2(p0.x + segW * (selT + 1.0f) - inset, p1.y - inset),
                      roleU32(palette.accent, kSegSelA), rnd);

    const ImVec4 selInk = ui::maxContrastTextOn(roleVec(palette.accent, kSegSelA));
    bool         changed = false;
    for (int i = 0; i < count; ++i) {
        const ImVec2 sMin(p0.x + segW * (float)i, p0.y);
        const ImVec2 sMax(sMin.x + segW, p1.y);

        ImGui::PushID(i);
        ImGui::SetCursorScreenPos(sMin);
        if (ImGui::InvisibleButton("##seg", ImVec2(segW, h)) && *selected != i) {
            *selected = i;
            changed   = true;
        }
        IMGUI_TEST_ENGINE_ITEM_INFO(
            ImGui::GetItemID(), labels[i] ? labels[i] : "", GImGui->LastItemData.StatusFlags);
        const bool  hovered = ImGui::IsItemHovered();
        const float hv      = anim::toward(ImGui::GetID("##hv"), hovered ? 1.0f : 0.0f,
                                           anim::kFast);
        ImGui::PopID();

        // How "selected" this segment currently looks — 1 under the sliding pill, 0 away from it.
        const float on = ImClamp(1.0f - std::fabs(selT - (float)i), 0.0f, 1.0f);
        if (hv > kSettled && on < 1.0f) {
            dl->AddRectFilled(ImVec2(sMin.x + inset, sMin.y + inset),
                              ImVec2(sMax.x - inset, sMax.y - inset),
                              roleU32(palette.accent, kSegHoverA * hv * (1.0f - on)), rnd);
        } else if (i > 0 && on <= 0.0f) {
            dl->AddLine(ImVec2(sMin.x, sMin.y + inset * 2.0f),
                        ImVec2(sMin.x, sMax.y - inset * 2.0f), roleU32(palette.border),
                        ui::metrics::stroke(1.0f));
        }

        // Fit the label to its own segment BEFORE centring: centre-then-clip eats both ends and
        // leaves an unreadable middle fragment.
        const std::string lab = elide(labels[i] ? labels[i] : "", segW - inset * 2.0f);
        const ImVec2      ts  = ImGui::CalcTextSize(lab.c_str());
        const ImVec4      idle = roleVec(hovered ? palette.text : palette.textDim);
        dl->PushClipRect(ImVec2(sMin.x + inset, sMin.y), ImVec2(sMax.x - inset, sMax.y), true);
        dl->AddText(ImVec2(sMin.x + (segW - ts.x) * 0.5f, sMin.y + (h - ts.y) * 0.5f),
                    u32(lerpVec4(idle, selInk, on)), lab.c_str());
        dl->PopClipRect();

        if (hovered) {
            if (tooltips && tooltips[i] && tooltips[i][0])          kitTooltip(tooltips[i]);
            else if (labels[i] && lab != labels[i])                 kitTooltip(labels[i]);
        }
    }

    ImGui::SetCursorScreenPos(p0);
    ImGui::Dummy(ImVec2(w, h));
    ImGui::PopID();
    return changed;
}

bool popoverBegin(const char* id, const char* label, const char* icon, int activeCount) {
    ImGuiContext& g = *GImGui;
    ImGui::PushID(id && id[0] ? id : "kit_popover");

    const char*  lab    = label ? label : "";
    const float  h      = ImGui::GetFrameHeight();
    const float  pX     = padX();
    const float  gap    = ui::metrics::U(1.0f);
    const float  glyph  = (icon && icon[0]) ? ui::metrics::iconHeader() : 0.0f;
    const ImVec2 ts     = ImGui::CalcTextSize(lab);
    // The count badge lives INSIDE the button's top-right corner, so its width has to be
    // reserved here or it sits on top of the label. It used to be drawn hanging off the
    // button's outer edge, which put it above the row's clip rect and clipped its top half off.
    const std::string bnum   = activeCount > 0 ? fmtCount((long long)activeCount) : std::string();
    const float       badgeW =
        bnum.empty() ? 0.0f : ImGui::CalcTextSize(bnum.c_str()).x + ui::metrics::U(1.5f);
    const float  w      = pX * 2.0f + ts.x + (glyph > 0.0f ? glyph + gap : 0.0f) +
                          (badgeW > 0.0f ? badgeW + gap : 0.0f);

    const ImVec2 p0      = ImGui::GetCursorScreenPos();
    const bool   pressed = ImGui::InvisibleButton("##kit_poptrigger", ImVec2(w, h));
    IMGUI_TEST_ENGINE_ITEM_INFO(
        ImGui::GetItemID(), lab,
        GImGui->LastItemData.StatusFlags | ImGuiItemStatusFlags_Openable |
            (ImGui::IsPopupOpen("##kit_popover_body") ? ImGuiItemStatusFlags_Opened : 0));
    const bool   hovered = ImGui::IsItemHovered();
    const float  hv      = anim::toward(ImGui::GetID("##hv"), hovered ? 1.0f : 0.0f, anim::kFast);

    ImDrawList*  dl = ImGui::GetWindowDrawList();
    const ImVec2 p1(p0.x + w, p0.y + h);
    const float  rnd = chipRound();
    dl->AddRectFilled(p0, p1, u32(lerpVec4(roleVec(palette.button), roleVec(palette.buttonHovered),
                                           hv)), rnd);
    dl->AddRect(p0, p1, roleU32(activeCount > 0 ? palette.accent : palette.border,
                                kChipBorderA + (1.0f - kChipBorderA) * hv),
                rnd, 0, ui::metrics::stroke(1.0f));

    float x = p0.x + pX;
    if (glyph > 0.0f &&
        drawIconAt(dl, icon, ImVec2(x + glyph * 0.5f, p0.y + h * 0.5f), glyph,
                   roleU32(palette.text))) {
        x += glyph + gap;
    }
    dl->AddText(ImVec2(x, p0.y + (h - ts.y) * 0.5f), roleU32(palette.text), lab);

    // A collapsed popover must never hide the fact that its filters are ON. Vertically CENTRED and
    // fully inside the button, in the width reserved above: a corner badge that overhangs the
    // frame gets clipped by whatever row the button sits in, which is what happened on the PLM
    // graph toolbar (the count's top half was sliced off by the toolbar's own clip rect).
    if (!bnum.empty()) {
        const ImVec2 bs = ImGui::CalcTextSize(bnum.c_str());
        const float  bh = bs.y + ui::metrics::U(0.5f);
        const ImVec2 b1(p1.x - pX, p0.y + (h + bh) * 0.5f);
        const ImVec2 b0(b1.x - badgeW, b1.y - bh);
        dl->AddRectFilled(b0, b1, roleU32(palette.accent), bh * 0.5f);
        dl->AddText(ImVec2(b0.x + (badgeW - bs.x) * 0.5f, b0.y + (bh - bs.y) * 0.5f),
                    u32(ui::maxContrastTextOn(roleVec(palette.accent))), bnum.c_str());
    }

    if (pressed) ImGui::OpenPopup("##kit_popover_body");

    ui::pushGlassWindowStyle();
    ImGui::PushStyleVar(ImGuiStyleVar_PopupRounding, rowRound());
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(padX(), padY()));
    // Wrapped text and full-width fields cannot establish an auto-fit width from zero.
    // Keep the default popover readable without overriding caller-supplied constraints.
    if (ImGui::IsPopupOpen("##kit_popover_body") &&
        !(g.NextWindowData.Flags & ImGuiNextWindowDataFlags_HasSizeConstraint)) {
        const float minW = ImMin(ImMax(w, ImGui::GetFontSize() * 18.0f + padX() * 2.0f),
                                 ImGui::GetMainViewport()->WorkSize.x);
        ImGui::SetNextWindowSizeConstraints(ImVec2(minW, 0.0f), ImVec2(FLT_MAX, FLT_MAX));
    }
    const bool open = ImGui::BeginPopup("##kit_popover_body");
    if (!open) {
        ImGui::PopStyleVar(2);
        ui::popGlassWindowStyle();
        ImGui::PopID();
    }
    return open;
}

void popoverEnd() {
    ImGui::EndPopup();
    ImGui::PopStyleVar(2);
    ui::popGlassWindowStyle();
    ImGui::PopID();
}

// ==================================================================================================
// status
// ==================================================================================================
Tone statusTone(Status s) {
    switch (s) {
        case Status::Running: return Tone::Accent;
        case Status::Done:    return Tone::Ok;
        case Status::Failed:  return Tone::Err;
        case Status::Retry:   return Tone::Warn;
        case Status::Skipped: return Tone::Info;   // deliberately did not run: informational blue
        case Status::Pending:
        default:              return Tone::Neutral;
    }
}

// Ported from the copilot's drawStatusGlyph, which is the canonical one. `r` is the vector-mark
// radius; the brand mark's ink box is the enclosing glyph CELL (r / 0.30), exactly the ratio the
// original's legibility ladder was measured at — so statusGlyphInline reproduces it pixel for
// pixel, and a caller that reserves the cell gets the same art everywhere.
void statusGlyph(ImDrawList* dl, ImVec2 center, float r, Status s, const AdaptiveSurface& surf) {
    if (!dl || !(r > 0.0f)) return;

    const float box = r / kGlyphRadiusFrac;
    const ImU32 col = u32(s == Status::Pending ? surf.textTertiary : toneOn(statusTone(s), surf));
    const float th  = ui::metrics::stroke(1.6f);

    switch (s) {
        case Status::Running:
            // Busy IS the brand mark in motion, so a running step, the composer and the copilot
            // header are visibly the same object. Ambient motion never settles: pump the frames.
            anim::requestFrames();
            if (box >= ui::kTesseractSpinnerMinPx) {
                ui::drawTesseractSpinner(dl, center, box, ImGui::GetTime(), col);
            } else {
                // Below the mark's legibility floor a 16-vertex wireframe is a smudge, so the
                // original comet arc stays.
                const float t   = (float)ImGui::GetTime();
                const int   seg = 14;
                for (int i = 0; i < seg; ++i) {
                    const float f  = (float)i / (float)seg;
                    const float aa = t * 6.0f + f * 2.0f * IM_PI;
                    const float al = 0.12f + 0.88f * f;
                    const ImU32 cc = (col & 0x00FFFFFFu) | ((ImU32)(al * 255.0f) << 24);
                    dl->AddCircleFilled(
                        ImVec2(center.x + std::cos(aa) * r, center.y + std::sin(aa) * r), th, cc);
                }
            }
            break;
        case Status::Done:                                        // check mark
            dl->AddLine(ImVec2(center.x - r * 0.55f, center.y + r * 0.05f),
                        ImVec2(center.x - r * 0.10f, center.y + r * 0.50f), col, th);
            dl->AddLine(ImVec2(center.x - r * 0.10f, center.y + r * 0.50f),
                        ImVec2(center.x + r * 0.60f, center.y - r * 0.50f), col, th);
            break;
        case Status::Failed:                                      // cross
            dl->AddLine(ImVec2(center.x - r * 0.5f, center.y - r * 0.5f),
                        ImVec2(center.x + r * 0.5f, center.y + r * 0.5f), col, th);
            dl->AddLine(ImVec2(center.x - r * 0.5f, center.y + r * 0.5f),
                        ImVec2(center.x + r * 0.5f, center.y - r * 0.5f), col, th);
            break;
        case Status::Retry:                                       // ring
            dl->AddCircle(center, r, col, 12, th);  // gui-lint: allow 12 = num_segments, not px
            break;
        case Status::Skipped:                                     // dash: nothing ran, nothing broke
            dl->AddLine(ImVec2(center.x - r * 0.6f, center.y), ImVec2(center.x + r * 0.6f, center.y),
                        col, th);
            break;
        case Status::Pending:
        default:
            // Pending hollow dot.
            dl->AddCircle(center, r * 0.8f, col, 12, th);  // gui-lint: allow 12 = num_segments
            break;
    }
}

void statusGlyphInline(Status s) {
    const float  box = ImGui::GetTextLineHeight();
    const ImVec2 p0  = ImGui::GetCursorScreenPos();
    statusGlyph(ImGui::GetWindowDrawList(), ImVec2(p0.x + box * 0.5f, p0.y + box * 0.5f),
                box * kGlyphRadiusFrac, s, inkSurface());
    ImGui::Dummy(ImVec2(box, box));
}

void inlineStatus(const char* msg, Tone t) {
    if (!msg || !msg[0]) return;   // sticky-error callers pass an empty string unconditionally
    ImGuiContext& g = *GImGui;

    ImDrawList*  dl   = ImGui::GetWindowDrawList();
    const float  dotR = ui::metrics::U(0.75f);
    const float  txtH = ImGui::GetTextLineHeight();
    const ImVec2 p    = ImGui::GetCursorScreenPos();
    dl->AddCircleFilled(ImVec2(p.x + dotR, p.y + txtH * 0.5f), dotR, toneU32(t));
    ImGui::Dummy(ImVec2(dotR * 2.0f, txtH));
    ImGui::SameLine(0.0f, ui::metrics::U(1.0f));

    ImGui::PushStyleColor(ImGuiCol_Text, toneVec(t));
    ImGui::TextWrapped("%s", msg);
    IMGUI_TEST_ENGINE_ITEM_INFO(0, msg, g.LastItemData.StatusFlags);
    ImGui::PopStyleColor();
}

void progressBar(float frac, const char* caption, Tone t, float height) {
    ImGui::PushID(caption && caption[0] ? caption : "kit_progress");

    const float availW = ImMax(ImGui::GetContentRegionAvail().x, ui::metrics::stroke(1.0f));
    const float barH   = height > 0.0f ? height : ui::metrics::reProgressH();
    const bool  hasCap = caption && caption[0];

    ImVec2 capSz(0.0f, 0.0f);
    float  capGap = 0.0f;
    if (hasCap) {
        capSz  = ImGui::CalcTextSize(caption);
        capGap = ui::metrics::U(1.5f);
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

    const ImU32 fill = toneU32(t);
    // Anything outside [0,1] — including NaN — is a started job with no reported progress. A
    // marquee says "running"; an empty or full bar would lie about it.
    if (!(frac >= 0.0f && frac <= 1.0f)) {
        const float segW = barW * kMarqueeFrac;
        const float span = barW + segW;
        const float head = anim::phase() * span - segW;
        const float a    = p0.x + ImMax(0.0f, head);
        const float b    = p0.x + ImMin(barW, head + segW);
        if (b > a) dl->AddRectFilled(ImVec2(a, by), ImVec2(b, by + barH), fill, rnd);
    } else {
        // Eased, so a jump from 20% to 80% SWEEPS and the eye can see it moved.
        const float f = anim::toward(ImGui::GetID("##frac"), frac, anim::kBase);
        const float w = barW * ImClamp(f, 0.0f, 1.0f);
        if (w > 0.0f) dl->AddRectFilled(ImVec2(p0.x, by), ImVec2(p0.x + w, by + barH), fill, rnd);
    }

    if (hasCap) {
        dl->AddText(ImVec2(p0.x + availW - capSz.x, p0.y + (rowH - capSz.y) * 0.5f),
                    t == Tone::Neutral ? roleU32(palette.textDim) : toneU32(t), caption);
    }
    ImGui::PopID();
}

void spinner(const char* label, Tone t) {
    anim::requestFrames();   // the mark turns off GetTime(); without this it stutters at 20 Hz
    ui::busyLabel(label ? label : "", toneU32(t));
}

// ==================================================================================================
// empty / blocked states
// ==================================================================================================
bool emptyState(const char* icon, const char* title, const char* body, const char* actionLabel,
                bool actionEnabled) {
    ImGui::PushID(title && title[0] ? title : "kit_empty");

    ImGui::Dummy(ImVec2(0.0f, sectionGap()));
    if (icon && icon[0]) {
        const float  sz      = ui::metrics::iconHeader() * kEmptyIconScale;
        const float  availW  = ImGui::GetContentRegionAvail().x;
        const ImVec2 p       = ImGui::GetCursorScreenPos();
        if (drawIconAt(ImGui::GetWindowDrawList(), icon,
                       ImVec2(p.x + availW * 0.5f, p.y + sz * 0.5f), sz,
                       roleU32(palette.textDisabled, kEmptyIconA))) {
            ImGui::Dummy(ImVec2(availW, sz));
            ImGui::Dummy(ImVec2(0.0f, headGap()));
        }
    }

    centeredOrWrapped(title, roleVec(palette.text));
    if (body && body[0]) {
        ImGui::Dummy(ImVec2(0.0f, headGap()));
        centeredOrWrapped(body, roleVec(palette.textDim));
    }

    // The action sits directly beneath the text it resolves (a heading gap, not a section gap).
    bool clicked = false;
    if (actionLabel && actionLabel[0]) {
        ImGui::Dummy(ImVec2(0.0f, headGap()));
        clicked = centeredAction(actionLabel, actionEnabled);
    }

    ImGui::Dummy(ImVec2(0.0f, sectionGap()));
    ImGui::PopID();
    return clicked;
}

bool blockedState(const char* title, const char* reason, const char* hint,
                  const char* actionLabel) {
    ImGui::PushID(title && title[0] ? title : "kit_blocked");

    ImGui::Dummy(ImVec2(0.0f, sectionGap()));
    {
        const float  sz     = ui::metrics::iconHeader() * kEmptyIconScale;
        const float  availW = ImGui::GetContentRegionAvail().x;
        const ImVec2 p      = ImGui::GetCursorScreenPos();
        drawWarnGlyph(ImGui::GetWindowDrawList(), ImVec2(p.x + availW * 0.5f, p.y + sz * 0.5f), sz,
                      toneU32(Tone::Err));
        ImGui::Dummy(ImVec2(availW, sz));
        ImGui::Dummy(ImVec2(0.0f, headGap()));
    }

    centeredOrWrapped(title, toneVec(Tone::Err));
    if (reason && reason[0]) {
        ImGui::Dummy(ImVec2(0.0f, headGap()));
        centeredOrWrapped(reason, roleVec(palette.text));
    }
    if (hint && hint[0]) {
        ImGui::Dummy(ImVec2(0.0f, headGap()));
        centeredOrWrapped(hint, roleVec(palette.textDim));
    }

    bool clicked = false;
    if (actionLabel && actionLabel[0]) {
        ImGui::Dummy(ImVec2(0.0f, sectionGap()));
        clicked = centeredAction(actionLabel, true);
    }

    ImGui::Dummy(ImVec2(0.0f, sectionGap()));
    ImGui::PopID();
    return clicked;
}

// ==================================================================================================
// text
// ==================================================================================================
void dimText(const char* text) {
    if (!text || !text[0]) return;
    // Dim = the CURRENT ink at reduced alpha, so this stays readable on ANY surface: theme text on
    // opaque panels, adaptive glass ink inside a bubble. A fixed palette.textDim washes out there.
    ImVec4 c = ImGui::GetStyleColorVec4(ImGuiCol_Text);
    c.w *= kDimA;
    ImGui::PushStyleColor(ImGuiCol_Text, c);
    ImGui::TextWrapped("%s", text);
    ImGui::PopStyleColor();
}

namespace detail {
// Word-wrap `text` (newlines/tabs folded to spaces) into at most `maxLines` lines of `maxW` px.
// The last allowed line carries the remainder, elided; *cut reports that elision. Shared by the
// key/value row (kit.cpp) and the footer status / grouped picker (kit_panel.cpp) so wrapped
// kit text breaks identically everywhere.
std::vector<std::string> wrapLines(const char* text, float maxW, int maxLines, bool* cut) {
    std::vector<std::string> out;
    if (cut) *cut = false;
    if (!text || !text[0] || maxW <= 0.0f || maxLines <= 0) return out;
    std::string flat(text);
    for (char& c : flat)
        if (c == '\n' || c == '\r' || c == '\t') c = ' ';
    ImFont*     font  = ImGui::GetFont();
    const float scale = ImGui::GetFontSize() / ImMax(font->FontSize, 1.0f);
    const char* s     = flat.c_str();
    const char* end   = s + flat.size();
    while (end > s && end[-1] == ' ') --end;
    while (s < end && *s == ' ') ++s;
    while (s < end) {
        if ((int)out.size() + 1 == maxLines) {
            const std::string rest(s, end);
            std::string       shown = elide(rest.c_str(), maxW);
            if (cut && shown != rest) *cut = true;
            out.push_back(std::move(shown));
            break;
        }
        const char* e = font->CalcWordWrapPositionA(scale, s, end, maxW);
        if (e <= s) {   // narrower than one glyph: take one UTF-8 character
            unsigned int c = 0;
            e = s + ImMax(1, ImTextCharFromUtf8(&c, s, end));
        }
        const char* le = e;
        while (le > s && le[-1] == ' ') --le;
        out.emplace_back(s, le);
        s = e;
        while (s < end && *s == ' ') ++s;
    }
    return out;
}

// keyValueRow with an explicit key column (kit_plot.cpp keyValueCard aligns all of a card's rows
// on its widest key). labelW > 0: the value starts exactly labelW into the row. labelW <= 0: the
// standard pmLabelCol, which a longer key may widen for its own row. Either way the column is
// capped at kKeyColMaxFrac of the row, and a key longer than the column WRAPS onto a second line
// (eliding only past two lines, full key on hover) — "Max von Mises at fixtures" stays readable.
//
// The value sits on the key's first line. The optional copy button is a text-height icon button
// pinned to the row's right edge on that line, so a copyable row has the same rhythm as a plain
// one and the copy glyphs of a card line up in one column instead of trailing each value.
void keyValueRowCol(const char* key, const char* value, Tone valueTone, bool copyable, float labelW) {
    const char* k = key ? key : "";
    const char* v = value ? value : "";
    ImGui::PushID(k);

    const ImVec2 rowPos = ImGui::GetCursorScreenPos();
    const float  minW   = ui::metrics::stroke(1.0f);
    const float  rowW   = ImMax(ImGui::GetContentRegionAvail().x, minW);
    const float  lineH  = ImGui::GetTextLineHeight();
    const float  gap    = ImGui::GetStyle().ItemSpacing.x;

    // Default column = the pane-wide field value column (kit_form), so readouts line up with the
    // fields of the same pane; pmLabelCol when the pane column is unusable here.
    const float paneCol = fieldValueColumnX() - rowPos.x;
    float       col     = labelW > 0.0f ? labelW
                                        : (paneCol > gap ? paneCol : ui::metrics::pmLabelCol());
    if (labelW <= 0.0f) col = ImMax(col, ImGui::CalcTextSize(k).x + gap);
    col = ImMin(col, rowW * kKeyColMaxFrac);

    const float keyMax = ImMax(col - gap, minW);
    bool        keyCut = false;
    std::string keyShown;
    if (ImGui::CalcTextSize(k).x <= keyMax) {
        keyShown = k;
    } else {
        const std::vector<std::string> lines = wrapLines(k, keyMax, 2, &keyCut);
        for (size_t i = 0; i < lines.size(); ++i) keyShown += (i ? "\n" : "") + lines[i];
    }
    ImGui::PushStyleColor(ImGuiCol_Text, roleVec(palette.textDim));
    ImGui::TextUnformatted(keyShown.c_str());
    ImGui::PopStyleColor();
    if (ImGui::IsItemHovered() && keyCut) kitTooltip(k);

    const float btnS = copyable ? lineH : 0.0f;
    const float btnG = copyable ? ui::metrics::U(1.0f) : 0.0f;
    ImGui::SameLine(0.0f, 0.0f);
    ImGui::SetCursorScreenPos(ImVec2(rowPos.x + col, rowPos.y));
    // The whole point of this row: an id or an absolute path has no whitespace, so TextWrapped
    // breaks it mid-token inside a stretch column. Elide + tooltip instead, always.
    const std::string shown = elide(v, ImMax(rowW - col - btnS - btnG, minW));
    // Neutral reads as PRIMARY text here: the key is already dim, so dimming the value too makes
    // a perfectly good readout look disabled.
    ImGui::PushStyleColor(ImGuiCol_Text,
                          valueTone == Tone::Neutral ? roleVec(palette.text) : toneVec(valueTone));
    ImGui::TextUnformatted(shown.c_str());
    ImGui::PopStyleColor();
    if (ImGui::IsItemHovered() && shown != v) kitTooltip(v);

    if (copyable) {
        ImGui::SameLine(0.0f, 0.0f);
        ImGui::SetCursorScreenPos(ImVec2(rowPos.x + rowW - btnS, rowPos.y));
        if (iconButton("copy", "copy", "Copy to clipboard", v[0] != '\0', Tone::Neutral, btnS))
            ImGui::SetClipboardText(v);
    }
    ImGui::PopID();
}
}  // namespace detail

void keyValueRow(const char* key, const char* value, Tone valueTone, bool copyable) {
    detail::keyValueRowCol(key, value, valueTone, copyable, 0.0f);
}

std::string elide(const char* text, float maxW) {
    if (!text || !text[0] || maxW <= 0.0f) return std::string();
    if (ImGui::CalcTextSize(text).x <= maxW) return std::string(text);

    // Three ASCII periods, NOT U+2026: the app font has no ellipsis codepoint in every atlas
    // range it ships, and a missing glyph renders as a literal '?'.
    static const char* kEll = "...";
    const float        ellW = ImGui::CalcTextSize(kEll).x;
    if (ellW >= maxW) return std::string();

    const std::string s(text);
    // Longest prefix (on a UTF-8 boundary) whose width plus the ellipsis still fits.
    std::size_t lo = 0, hi = s.size();
    while (lo < hi) {
        std::size_t mid = lo + (hi - lo + 1) / 2;
        while (mid > lo && (static_cast<unsigned char>(s[mid]) & 0xC0) == 0x80) --mid;
        if (mid == lo) break;
        const float w = ImGui::CalcTextSize(s.c_str(), s.c_str() + mid).x + ellW;
        if (w <= maxW) lo = mid;
        else           hi = mid - 1;
    }
    if (lo == 0) return std::string(kEll);
    return s.substr(0, lo) + kEll;
}

// ==================================================================================================
// rhythm
// ==================================================================================================
void rule() {
    ImDrawList*  dl = ImGui::GetWindowDrawList();
    const float  pY = padY() * 0.5f;   // the hairline honours the active vertical padding
    const ImVec2 p  = ImGui::GetCursorScreenPos();
    const float  w  = ImMax(ImGui::GetContentRegionAvail().x, ui::metrics::stroke(1.0f));
    const float  t  = ui::metrics::stroke(1.0f);
    // The CURRENT ink at low alpha, not palette.border: a fixed mid-grey vanishes wherever a glass
    // pane's backdrop darkens to that grey (the footer rule "stopped" part-way across), while an
    // ink wash always shifts the surface toward the text colour, whatever is behind the glass.
    ImVec4 ink = ImGui::GetStyleColorVec4(ImGuiCol_Text);
    ink.w *= kRuleInkA;
    dl->AddLine(ImVec2(p.x, p.y + pY + t * 0.5f), ImVec2(p.x + w, p.y + pY + t * 0.5f),
                ImGui::ColorConvertFloat4ToU32(ink), t);
    ImGui::Dummy(ImVec2(w, t + pY * 2.0f));
}

void gapRow()     { ImGui::Dummy(ImVec2(0.0f, rowGap())); }
void gapSection() { ImGui::Dummy(ImVec2(0.0f, sectionGap())); }

// ==================================================================================================
// formatting
// ==================================================================================================
std::string fmtBytes(unsigned long long bytes) {
    char buf[64];
    if (bytes < 1024ull) {
        std::snprintf(buf, sizeof(buf), "%llu B", bytes);
        return std::string(buf);
    }
    double v = (double)bytes / 1024.0;
    if (v < 1024.0) {   // KB is a whole number: "512 KB", never "512.0 KB"
        std::snprintf(buf, sizeof(buf), "%.0f KB", v);
        return std::string(buf);
    }
    static const char* kUnits[] = {"MB", "GB", "TB", "PB"};
    int    u = 0;
    v /= 1024.0;
    while (v >= 1024.0 && u < 3) {
        v /= 1024.0;
        ++u;
    }
    std::snprintf(buf, sizeof(buf), "%.1f %s", v, kUnits[u]);
    return std::string(buf);
}

std::string fmtAgo(double unixSeconds) {
    // A zero/negative stamp is "never recorded", not "1970" — render nothing rather than a lie.
    if (!(unixSeconds > 0.0)) return std::string();

    const double d = (double)std::time(nullptr) - unixSeconds;
    if (d < 10.0) return std::string("just now");   // also covers a clock-skewed future stamp

    char buf[64];
    if (d < 60.0)         std::snprintf(buf, sizeof(buf), "%d s ago",   (int)d);
    else if (d < 3600.0)  std::snprintf(buf, sizeof(buf), "%d min ago", (int)(d / 60.0));
    else if (d < 86400.0) std::snprintf(buf, sizeof(buf), "%d h ago",   (int)(d / 3600.0));
    else                  std::snprintf(buf, sizeof(buf), "%d d ago",   (int)(d / 86400.0));
    return std::string(buf);
}

std::string fmtCount(long long n) {
    char buf[64];
    const bool      neg = n < 0;
    unsigned long long a = neg ? (unsigned long long)(-(n + 1)) + 1ull : (unsigned long long)n;

    if (a < 1000ull) {
        std::snprintf(buf, sizeof(buf), "%lld", n);
        return std::string(buf);
    }
    static const char* kUnits[] = {"", "k", "M", "B", "T"};
    double v = (double)a;
    int    u = 0;
    // 999.95 rather than 1000: a value that would PRINT as "1000.0k" promotes to "1.0M" instead.
    while (v >= 999.95 && u < 4) {
        v /= 1000.0;
        ++u;
    }
    const bool whole = std::fabs(v - std::floor(v + 0.5)) < 0.05;
    if (whole) std::snprintf(buf, sizeof(buf), "%s%.0f%s", neg ? "-" : "", v, kUnits[u]);
    else       std::snprintf(buf, sizeof(buf), "%s%.1f%s", neg ? "-" : "", v, kUnits[u]);
    return std::string(buf);
}

std::string fmtDuration(double seconds) {
    char buf[64];
    if (!(seconds > 0.0)) return std::string("0.0 s");   // also catches NaN

    const long total = (long)(seconds + 0.5);
    if (total < 60)        std::snprintf(buf, sizeof(buf), "%.1f s", seconds);
    else if (total < 3600) std::snprintf(buf, sizeof(buf), "%ld m %02ld s", total / 60, total % 60);
    else                   std::snprintf(buf, sizeof(buf), "%ld h %02ld m", total / 3600,
                                         (total % 3600) / 60);
    return std::string(buf);
}

}  // namespace kit
}  // namespace ui
