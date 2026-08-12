// reveng_widgets.cpp — the RE-40 shared visual kit for the Reverse Engineering workspace.
// See reveng_widgets.h for the contract each widget owes its callers.
//
// WHY MOST OF THIS FILE IS NOW FORWARDERS
// This TU used to be the best-behaved widget kit in the app, which is exactly why
// ui/widgets/kit.h was modelled on it: kit.cpp absorbed this file's card chrome, section
// header, chips, buttons, segmented control, progress bar and empty/blocked states. Keeping
// both would leave the product with two implementations of every one of them — the precise
// failure kit.h exists to end. The shared widgets therefore FORWARD to ui::kit wrapped in
// Density::Roomy, whose padding/gap/corner tokens ARE the re* family, so the RE tab keeps its
// own rhythm while the pixels come from one painter.
//
// What is still implemented here is only what the app-wide kit has no business owning: the
// honesty tag, the image / 3D previews and their slot publication, the capability row, the
// stage StepTrack, the two-column field label and the mm / % formatters.
//
// House rules this TU obeys:
//   • Every colour comes from levelColor()/levelColorVec(), a ui::kit tone or a ui::palette
//     role. No literals.
//   • Every dimension comes from ui::metrics::* (the re* tokens where one exists). No literals.
//   • Every busy indicator is the ui::kit Running mark. This file no longer owns a spinner.
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
#include "../metrics.h"
#include "ui/anim.h"          // requestFrames — the stepper's active-node halo is ambient motion
#include "ui/tesseract.h"     // kTesseractSpinnerMinPx — the brand mark's legibility floor
#include "ui/widgets/kit.h"   // the app-wide vocabulary every shared widget below forwards to

namespace ui {
namespace reveng {
namespace kit {
namespace {

// ---- dp vs. scaled-pixel radii ------------------------------------------------------------------
// ui::glassBackground / glassSelectionGlow take a LOGICAL dp radius (they multiply by ui::uiScale
// themselves) while ui::metrics::re*Round() already returns scaled pixels. Convert back exactly
// once so the corner never double-scales at HiDPI. ImDrawList::Add*() rounding, by contrast, is
// raw pixels — pass the metrics token straight through there.
inline float dpOf(float scaledPx) {
    const float s = ui::metrics::scale();
    return s > 0.0f ? scaledPx / s : scaledPx;
}
inline float cardRoundDp() { return dpOf(ui::metrics::reCardRound()); }
inline float chipRoundDp() { return dpOf(ui::metrics::reChipRound()); }

// ---- opacity / ratio constants ------------------------------------------------------------------
// These scale a palette role's alpha or are pure ratios; the COLOUR itself is always a palette role.
constexpr float kChipFillA     = 0.16f;
constexpr float kChipBorderA   = 0.55f;
constexpr float kMatteA        = 0.85f;   // plate behind a letterboxed image
constexpr float kPlaceholderA  = 0.35f;
constexpr float kCaptionA      = 0.80f;
constexpr float kHoverRingA    = 0.55f;
constexpr float kTileBaseA     = 0.45f;
constexpr float kTileHoverA    = 0.70f;
constexpr float kNodeIdleA     = 0.45f;
constexpr float kNodeHaloA     = 0.28f;
constexpr float kStripA        = 0.72f;

constexpr float kTooltipWrapEm  = 26.0f;      // tooltip wrap width, in em
constexpr float kHeroAspect     = 9.0f / 16.0f;
constexpr float kPulseBase      = 1.55f;      // active step-node halo radius, x node radius
constexpr float kPulseAmp       = 0.18f;
constexpr float kPulseSpeed     = 3.0f;
constexpr int   kMaxDecimals    = 6;

// ui::kit::statusGlyph takes the VECTOR-MARK radius and derives the brand mark's ink box as
// r / 0.30 (kit.h, "status" section). Callers that own a box and want the mark to fill it convert
// through this ratio — it is part of that documented contract, not a fresh magic number.
constexpr float kKitGlyphRadiusFrac = 0.30f;

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

// ---- Level -> ui::kit::Tone ---------------------------------------------------------------------
// The two enums are the same five meanings; this is the ONLY place the mapping lives, so a
// Level::Warn chip and a copilot Tone::Warn chip can never drift apart. Busy maps to Accent and
// gains the kit's Running mark on top (see overlayBusyMark) — the kit tone alone cannot say
// "still in flight", and an RE panel that showed a motionless busy chip would be lying.
inline ui::kit::Tone toneOf(Level l) {
    switch (l) {
        case Level::Ok:    return ui::kit::Tone::Ok;
        case Level::Warn:  return ui::kit::Tone::Warn;
        case Level::Error: return ui::kit::Tone::Err;
        case Level::Busy:  return ui::kit::Tone::Accent;
        case Level::Neutral:
        default:           return ui::kit::Tone::Neutral;
    }
}

// ui::kit::statusGlyph resolves its ink through the surface's darkText bit only (via toneOn), so
// deriving that one bit from the CURRENT text colour is exactly as correct here as a full
// adaptivePanel() sample — and does not add a hysteretic luminance sampler per chip.
ui::AdaptiveSurface glyphSurface() {
    ui::AdaptiveSurface s;
    const ImVec4 ink = ImGui::GetStyleColorVec4(ImGuiCol_Text);
    s.darkText       = (0.2126f * ink.x + 0.7152f * ink.y + 0.0722f * ink.z) < 0.5f;
    s.textPrimary    = ink;
    s.textSecondary  = ink;
    s.textTertiary   = ink;
    return s;
}

// The app-wide Running mark drawn over the tone dot ui::kit just painted. `center` is that dot's
// centre and `dotR` its radius; the mark rides just OUTSIDE the disc (the same trick StepTrack
// uses for a busy step node) so nothing has to be erased and the accent dot still reads.
void overlayBusyMark(ImVec2 center, float dotR) {
    ui::kit::statusGlyph(ImGui::GetWindowDrawList(), center, dotR + ui::metrics::stroke(1.5f),
                         ui::kit::Status::Running, glyphSurface());
}

// Centre of the tone dot inside a chip whose top-left is `p0`. Mirrors ui::kit's chip geometry
// (horizontal pad U(1.5), vertical pad U(0.5), dot radius U(0.75)) so the busy mark lands on the
// dot it decorates. Derived from the tokens rather than read back off the item rect: ui::kit::chip
// may open a tooltip as its last act, which leaves ImGui's "last item" pointing inside that popup.
constexpr float kKitChipPadXU = 1.5f;
constexpr float kKitChipPadYU = 0.5f;
constexpr float kKitChipDotRU = 0.75f;
inline float chipDotR() { return ui::metrics::U(kKitChipDotRU); }
inline ImVec2 chipDotCenter(const ImVec2& p0) {
    const float h = ImGui::GetTextLineHeight() + ui::metrics::U(kKitChipPadYU) * 2.0f;
    return ImVec2(p0.x + ui::metrics::U(kKitChipPadXU) + chipDotR(), p0.y + h * 0.5f);
}

// ---- small painters -----------------------------------------------------------------------------
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

// ---- chip geometry (HonestyTag, the thumbnail quality badge, CapabilityRow's right-align) -------
struct ChipGeom {
    float  w = 0.0f, h = 0.0f;
    float  padX = 0.0f, padY = 0.0f, gap = 0.0f, dotR = 0.0f;
    ImVec2 textSize = ImVec2(0.0f, 0.0f);
};

ChipGeom chipGeom(const char* label, bool withDot) {
    ChipGeom g;
    g.padX = ui::metrics::U(kKitChipPadXU);
    g.padY = ui::metrics::U(kKitChipPadYU);
    g.gap  = ui::metrics::reLabelGap();
    g.dotR = withDot ? chipDotR() : 0.0f;
    g.textSize = ImGui::CalcTextSize(label ? label : "");
    g.h = g.textSize.y + g.padY * 2.0f;
    g.w = g.padX * 2.0f + g.textSize.x + (withDot ? (g.dotR * 2.0f + g.gap) : 0.0f);
    return g;
}

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

// ---- wrapped paragraph (CapabilityRow's always-visible fix-it hint) -----------------------------
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

// The RE tab's ambient density. Every forwarder below opens it so ui::kit resolves padX/padY/
// rowGap/rowRound/chipRound/sectionGap/headGap from the re* tokens this workspace was tuned on.
struct Roomy {
    Roomy()  { ui::kit::pushDensity(ui::kit::Density::Roomy); }
    ~Roomy() { ui::kit::popDensity(); }
    Roomy(const Roomy&)            = delete;
    Roomy& operator=(const Roomy&) = delete;
};

}  // namespace

// =================================================================================================
// semantic status — thin wrappers over the ui::kit tone table
// =================================================================================================
ImU32 levelColor(Level l) { return ui::kit::toneU32(toneOf(l)); }

ImVec4 levelColorVec(Level l) { return ui::kit::toneVec(toneOf(l)); }

// =================================================================================================
// cards -> ui::kit::cardBegin / cardEnd / cardHeaderRight
// =================================================================================================
bool CardBegin(const char* id, const char* title, const char* icon, bool collapsible, bool* open) {
    // Supplying `open` implies the card can collapse, even if the caller left `collapsible` false.
    const bool canCollapse = collapsible || open != nullptr;

    // ui::kit::cardBegin collapses through a caller-owned bool. This kit's older contract also
    // allowed `collapsible` with no bool at all, so that case still needs somewhere to live: the
    // PARENT window's storage, resolved BEFORE the kit opens its child (whose id stack is fresh).
    ImGuiStorage* store = nullptr;
    ImGuiID       key   = 0;
    bool          local = true;
    if (canCollapse && !open) {
        store = ImGui::GetStateStorage();
        ImGui::PushID(id ? id : "re_card");
        key = ImGui::GetID("re_card_open");
        ImGui::PopID();
        local = store->GetBool(key, true);
    }

    ui::kit::pushDensity(ui::kit::Density::Roomy);   // paired in CardEnd()
    const bool visible =
        ui::kit::cardBegin(id, title, icon, canCollapse ? (open ? open : &local) : nullptr);
    if (store) store->SetBool(key, local);
    return visible;
}

void CardEnd() {
    ui::kit::cardEnd();
    ui::kit::popDensity();
}

void CardHeaderRight(float widgetsWidth) { ui::kit::cardHeaderRight(widgetsWidth); }

void SectionHeader(const char* label, const char* icon) {
    const Roomy density;
    // `first` suppresses ui::kit's leading sectionGap: RE panels already place their own gap above
    // a header (GapSection, or the card's own top padding), so letting the kit add one doubles it.
    ui::kit::sectionHeader(label, icon, nullptr, /*count=*/-1, /*first=*/true);
}

// =================================================================================================
// chips / badges
// =================================================================================================
void StatusChip(const char* label, Level l, const char* tooltip) {
    const Roomy  density;
    const ImVec2 p0 = ImGui::GetCursorScreenPos();
    ui::kit::chip(label, toneOf(l), /*dot=*/true, tooltip);
    if (l == Level::Busy)
        overlayBusyMark(chipDotCenter(p0), chipDotR());
}

void ValueChip(const char* label, const char* value, Level l, const char* tooltip) {
    const Roomy density;
    ui::kit::valueChip(label, value, toneOf(l), tooltip);
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
    // Deliberately NOT ui::kit::chip: the full-strength border is the whole signal, and a tier is
    // a permanent property of the value, not a status that will change on the next poll.
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

    const Roomy  density;
    const float  dotR = chipDotR();
    const ImVec2 p    = ImGui::GetCursorScreenPos();
    ui::kit::inlineStatus(msg, toneOf(l));
    if (l == Level::Busy)
        overlayBusyMark(ImVec2(p.x + dotR, p.y + ImGui::GetTextLineHeight() * 0.5f), dotR);
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
// FieldLabel stays a real implementation: ui::kit has no two-column FORM row (keyValueRow is
// read-only and sets no item width), and inventing one for a single workspace is how the app grew
// four kits in the first place. It is 12 lines of cursor arithmetic, not a painter.
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
    const Roomy density;
    ui::kit::keyValueRow(key, value, toneOf(valueLevel), /*copyable=*/false);
}

// =================================================================================================
// buttons
// =================================================================================================
// reBtnH() (30 dp) is a hair shorter than ImGui's frame height at the shipping body font
// (17.5 dp + 2x7 dp padding = 31.5 dp), which is what ui::kit defaults to. Pass the RE height
// explicitly so no RE button changes size.
bool PrimaryButton(const char* label, bool enabled, const char* disabledReason,
                   const ImVec2& size) {
    const Roomy density;
    return ui::kit::primaryButton(label, enabled, disabledReason,
                                  ImVec2(size.x, size.y > 0.0f ? size.y : ui::metrics::reBtnH()));
}

bool SecondaryButton(const char* label, bool enabled, const char* disabledReason,
                     const ImVec2& size) {
    const Roomy density;
    return ui::kit::secondaryButton(label, enabled, disabledReason,
                                    ImVec2(size.x, size.y > 0.0f ? size.y : ui::metrics::reBtnH()));
}

bool DangerButton(const char* label, bool enabled, const char* disabledReason) {
    const Roomy density;
    return ui::kit::dangerButton(label, enabled, disabledReason,
                                 ImVec2(0.0f, ui::metrics::reBtnH()));
}

bool SegmentedControl(const char* id, const char* const* labels, int count, int* selected,
                      float width) {
    const Roomy density;
    // ui::kit::segmented sizes its height from ImGui's frame height and, with no explicit width,
    // to its own content. Both defaults differ from this control's contract (reBtnH, full pane
    // width), and only the height is unreachable through its arguments — so set FramePadding.y to
    // land GetFrameHeight() exactly on reBtnH() for the duration of the call.
    const float padY = ImMax(0.0f, (ui::metrics::reBtnH() - ImGui::GetFontSize()) * 0.5f);
    ImGui::PushStyleVar(ImGuiStyleVar_FramePadding,
                        ImVec2(ImGui::GetStyle().FramePadding.x, padY));
    const bool changed =
        ui::kit::segmented(id, labels, count, selected,
                           width > 0.0f ? width : ImGui::GetContentRegionAvail().x);
    ImGui::PopStyleVar();
    return changed;
}

// =================================================================================================
// progress
// =================================================================================================
void ProgressBar(float pct, const char* caption, Level l) {
    const Roomy density;
    // Level::Neutral means "no judgement about the OUTCOME", not "grey bar": an RE progress bar has
    // always been accent-filled unless a level overrides it, so Neutral maps to Tone::Accent here
    // rather than to Tone::Neutral (which the kit renders in textDim).
    ui::kit::progressBar(pct, caption, l == Level::Neutral ? ui::kit::Tone::Accent : toneOf(l));
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
            // Ambient motion: without the frame request this breathes at whatever cadence stray
            // mouse events produce, i.e. a 20 Hz stutter the moment the cursor stops.
            ui::anim::requestFrames();
            const float pulse =
                kPulseBase + kPulseAmp * (float)std::sin(ImGui::GetTime() * kPulseSpeed);
            dl->AddCircleFilled(c, nodeR * pulse, roleU32(palette.accent, kNodeHaloA));
        }
        // A busy node BECOMES the app's Running mark rather than wearing one: the mark fills its
        // whole box, so painting it over a filled disc in the same colour just merges into a blob.
        // Below the mark's legibility floor ui::kit falls back to a comet arc, so the disc stays.
        const float busyBox  = (nodeR + ui::metrics::stroke(2.5f)) * 2.0f;
        const bool  busyMark = lv == Level::Busy && busyBox >= ui::kTesseractSpinnerMinPx;
        if (!busyMark) {
            dl->AddCircleFilled(c, nodeR, roleU32(rl, lv == Level::Neutral ? kNodeIdleA : 1.0f));
            dl->AddCircle(c, nodeR, roleU32(rl, hovered ? 1.0f : kChipBorderA), 0,
                          ui::metrics::stroke(1.0f));
        }
        if (lv == Level::Busy) {
            ui::kit::statusGlyph(dl, c, busyBox * kKitGlyphRadiusFrac, ui::kit::Status::Running,
                                 glyphSurface());
        }

        // Fit the label to its own segment (minus a hair of breathing room) BEFORE centring.
        // Centre-then-clip would trim both ends and leave an unreadable middle fragment.
        const std::string lab = ui::kit::elide(labels[i] ? labels[i] : "",
                                              segW - ui::metrics::U(1.0f));
        const ImVec2      ts  = ImGui::CalcTextSize(lab.c_str());
        dl->PushClipRect(ImVec2(sMin.x, p0.y), ImVec2(sMin.x + segW, p0.y + h), true);
        dl->AddText(ImVec2(c.x - ts.x * 0.5f, labY),
                    roleU32((i == active || hovered) ? palette.text : palette.textDim),
                    lab.c_str());
        dl->PopClipRect();
        // Full text on hover, so an elided step is never ambiguous.
        if (hovered && labels[i] && lab != labels[i]) ImGui::SetTooltip("%s", labels[i]);
    }

    ImGui::SetCursorScreenPos(p0);
    ImGui::Dummy(ImVec2(availW, h));
    ImGui::PopID();
    return clicked;
}

// =================================================================================================
// previews — the RE tab's own art; ui::kit has no image/3D surface and should not grow one
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
// empty / error states -> ui::kit::emptyState / blockedState
// =================================================================================================
bool EmptyState(const char* icon, const char* title, const char* body, const char* actionLabel,
                bool actionEnabled) {
    const Roomy density;
    return ui::kit::emptyState(icon, title, body, actionLabel, actionEnabled);
}

bool BlockedState(const char* title, const char* reason, const char* hint,
                  const char* actionLabel) {
    const Roomy density;
    return ui::kit::blockedState(title, reason, hint, actionLabel);
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
    const Roomy density;
    ui::kit::rule();
}

void GapCards()   { const Roomy density; ui::kit::gapRow(); }
void GapSection() { const Roomy density; ui::kit::gapSection(); }
// No kit equivalent: reCtlGap (control -> next label) sits between reHeadGap and reCardGap, and a
// fourth rhythm token in the kit would only ever have this one caller.
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

// Byte-identical formats, so this is a pure alias.
std::string fmtDuration(double seconds) { return ui::kit::fmtDuration(seconds); }

}  // namespace kit
}  // namespace reveng
}  // namespace ui
