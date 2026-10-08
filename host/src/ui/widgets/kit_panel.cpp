// kit_panel.cpp — the ONE implementation of the panel scaffold declared in kit_panel.h: header,
// form tabs, stepper, scroll body + measured pinned footer, action footer, removable card,
// framed canvas and the grouped picker.
//
// House rules (same as kit.cpp): colors are ui::palette roles or tones, sizes are density
// accessors / ui::metrics tokens, motion eases through ui::anim, and every interactive item is
// scoped under the caller's explicit id.
//
// GL/GUI thread only.
#include "kit_panel.h"

#include <cmath>
#include <string>
#include <vector>

#include "imgui.h"
#include "imgui_internal.h"

#include "../anim.h"
#include "../icons.h"
#include "../metrics.h"
#include "kit.h"

namespace ui {
namespace kit {
namespace detail {
// kit.cpp: word-wrap into at most maxLines lines of maxW px, the last one elided (*cut).
std::vector<std::string> wrapLines(const char* text, float maxW, int maxLines, bool* cut);
}  // namespace detail
namespace {

// ---- ratios / opacities (the COLOR is always a palette role) ----------------------------------
constexpr float kTrackA       = 0.85f;   // segmented track, same as kit::segmented
constexpr float kSegSelA      = 0.85f;   // selected segment fill
constexpr float kSegHoverA    = 0.16f;   // hover wash
constexpr float kStepSelA     = 0.14f;   // current-step wash under the accent bar
constexpr float kChipFillA    = 0.16f;
constexpr float kChipBorderA  = 0.55f;
constexpr float kDimA         = 0.62f;
constexpr float kDisabledA    = 0.55f;
constexpr float kGlyphFrac    = 0.30f;   // status-mark radius as a fraction of a text line
constexpr float kSettled      = 0.004f;
constexpr float kPickerWDp    = 380.0f;  // default grouped-picker width (logical dp)
constexpr float kPickerMaxHFrac = 0.70f; // grouped picker never exceeds this share of the viewport
constexpr int   kPickerDescLines = 2;    // picker descriptions wrap to at most this many lines
constexpr int   kStatusLines  = 2;       // footer status / blocker: at most this many lines
constexpr float kFadeBottomU  = 3.5f;    // scroll-body "more below" fade height (metrics::U)
constexpr float kFadeTopU     = 2.0f;    // scroll-body "more above" fade height (metrics::U)
constexpr float kFadeChevronU = 1.0f;    // "more below" chevron half-width (metrics::U)
constexpr float kCanvasMinWDp = 160.0f;
constexpr float kCanvasMinHDp = 120.0f;
constexpr float kMinTitleEm   = 8.0f;    // a header title never elides below ~8 em ...
constexpr float kMinTitleFrac = 0.60f;   // ... or 60% of its full width (whichever is larger)

inline ImU32 role(const float c[4], float alphaMul = 1.0f) {
    float t[4];
    Palette::withAlpha(c, t, alphaMul);
    return (ImU32)Palette::toU32(t);
}
inline ImVec4 roleV(const float c[4], float alphaMul = 1.0f) {
    return ImVec4(c[0], c[1], c[2], c[3] * alphaMul);
}
inline ImVec4 lerpV(const ImVec4& a, const ImVec4& b, float t) {
    return ImVec4(a.x + (b.x - a.x) * t, a.y + (b.y - a.y) * t, a.z + (b.z - a.z) * t,
                  a.w + (b.w - a.w) * t);
}
inline ImU32 u32(const ImVec4& c) { return ImGui::ColorConvertFloat4ToU32(c); }

// The surface the pane paints on, derived from the ink the caller pushed (kit.cpp's rule): dark
// ink means a bright surface. Feeds the pill scrollbar and status marks.
AdaptiveSurface inkSurf() {
    AdaptiveSurface s;
    const ImVec4 ink = ImGui::GetStyleColorVec4(ImGuiCol_Text);
    const float  lum = 0.2126f * ink.x + 0.7152f * ink.y + 0.0722f * ink.z;
    s.darkText      = lum < 0.5f;
    s.textPrimary   = ink;
    s.textSecondary = ImVec4(ink.x, ink.y, ink.z, ink.w * 0.85f);
    s.textTertiary  = ImVec4(ink.x, ink.y, ink.z, ink.w * kDimA);
    return s;
}

// Atlas icon centred at `c`. SDF line art takes the role tint; multi-color raster art keeps its
// authored colors (identity tint, alpha-scaled when dimmed). Unknown names draw nothing.
bool paintIcon(ImDrawList* dl, const char* name, ImVec2 c, float sz, ImU32 sdfCol,
               float alphaMul = 1.0f) {
    if (!dl || !name || !name[0] || sz <= 0.0f) return false;
    if (ui::hasIconSdf(name)) {
        ui::drawIconGlass(dl, name, c, sz, sdfCol, 0);
        return true;
    }
    const IconUV ic = ui::getIcon(name, sz >= ui::metrics::iconHeader() ? 32 : 20);
    if (!ic.valid) return false;
    const float r    = sz * 0.5f;
    const ImU32 a    = (ImU32)(ImClamp(alphaMul, 0.0f, 1.0f) * 255.0f) << IM_COL32_A_SHIFT;
    const ImU32 tint = (IM_COL32_WHITE & ~IM_COL32_A_MASK) | a;
    dl->AddImage(ic.texId, ImVec2(c.x - r, c.y - r), ImVec2(c.x + r, c.y + r), ic.uv0, ic.uv1, tint);
    return true;
}

// kit::chip's geometry (kit.cpp chipGeom): pad U(1.5) x U(0.5), dot U(0.75) + U(1) gap.
float chipW(const char* label, bool dot) {
    return ui::metrics::U(1.5f) * 2.0f + ImGui::CalcTextSize(label ? label : "").x +
           (dot ? ui::metrics::U(0.75f) * 2.0f + ui::metrics::U(1.0f) : 0.0f);
}
float chipH() { return ImGui::GetTextLineHeight() + ui::metrics::U(0.5f) * 2.0f; }

// Small right-pointing chevron (vector, never a glyph) marking flow between stepper steps.
void paintChevron(ImDrawList* dl, ImVec2 c, float r, ImU32 col) {
    const ImVec2 pts[3] = {ImVec2(c.x - r * 0.5f, c.y - r), ImVec2(c.x + r * 0.5f, c.y),
                           ImVec2(c.x - r * 0.5f, c.y + r)};
    dl->AddPolyline(pts, 3, col, ImDrawFlags_None, ui::metrics::stroke(1.5f));
}
// Down-pointing twin: the scroll body's "more below" cue.
void paintChevronDown(ImDrawList* dl, ImVec2 c, float r, ImU32 col) {
    const ImVec2 pts[3] = {ImVec2(c.x - r, c.y - r * 0.5f), ImVec2(c.x, c.y + r * 0.5f),
                           ImVec2(c.x + r, c.y - r * 0.5f)};
    dl->AddPolyline(pts, 3, col, ImDrawFlags_None, ui::metrics::stroke(1.5f));
}

// Layout snapshot of the current window: an overlay item (a header-row icon button, the fade
// child) placed with SetCursorScreenPos must not move the cursor, grow the line or the content
// size of what the caller lays out next. Capture before, restore() after.
struct LayoutSnap {
    ImGuiWindow* w = nullptr;
    ImVec2       cursor, prevLine, maxPos, idealMax, currLine, prevLineSz;
    float        currBase = 0.0f, prevBase = 0.0f;
    bool         sameLine = false, setPos = false;
    LayoutSnap() : w(ImGui::GetCurrentWindow()) {
        const ImGuiWindowTempData& dc = w->DC;
        cursor     = dc.CursorPos;
        prevLine   = dc.CursorPosPrevLine;
        maxPos     = dc.CursorMaxPos;
        idealMax   = dc.IdealMaxPos;
        currLine   = dc.CurrLineSize;
        prevLineSz = dc.PrevLineSize;
        currBase   = dc.CurrLineTextBaseOffset;
        prevBase   = dc.PrevLineTextBaseOffset;
        sameLine   = dc.IsSameLine;
        setPos     = dc.IsSetPos;
    }
    void restore() const {
        ImGuiWindowTempData& dc = w->DC;
        dc.CursorPos              = cursor;
        dc.CursorPosPrevLine      = prevLine;
        dc.CursorMaxPos           = maxPos;
        dc.IdealMaxPos            = idealMax;
        dc.CurrLineSize           = currLine;
        dc.PrevLineSize           = prevLineSz;
        dc.CurrLineTextBaseOffset = currBase;
        dc.PrevLineTextBaseOffset = prevBase;
        dc.IsSameLine             = sameLine;
        dc.IsSetPos               = setPos;
    }
};

// Hollow ring for a not-yet-started sub-step, filled tone dot otherwise.
void paintStatusDot(ImDrawList* dl, ImVec2 c, float r, Status s) {
    if (s == Status::Pending)
        dl->AddCircle(c, r, role(palette.textDim), 12, ui::metrics::stroke(1.2f));  // gui-lint: allow 12 = num_segments
    else
        dl->AddCircleFilled(c, r, toneU32(statusTone(s)));
}

// Ready/attention wording for a status mark; the stepper's default tooltip legend so a hollow
// ring vs a filled dot is never unexplained.
const char* statusWord(Status s) {
    switch (s) {
        case Status::Done:    return "Ready";
        case Status::Retry:   return "Needs attention";
        case Status::Failed:  return "Blocking";
        case Status::Running: return "In progress";
        case Status::Skipped: return "Not applicable";
        case Status::Pending:
        default:              return "Not started";
    }
}

// Tooltip for a stepper item: the caller's text when given, else "Label: Ready" so the mark is
// always explained (and the full label shows when it was elided).
void stepTooltip(const StepItem& s) {
    if (s.tooltip && s.tooltip[0]) {
        tooltip(s.tooltip);
        return;
    }
    const std::string t = std::string(s.label ? s.label : "") + (s.label && s.label[0] ? ": " : "") +
                          statusWord(s.status);
    tooltip(t.c_str());
}

// One line of text with newlines folded to spaces (single-line elision needs a flat string).
std::string oneLine(const char* text) {
    std::string s = text ? text : "";
    for (char& c : s)
        if (c == '\n' || c == '\r' || c == '\t') c = ' ';
    return s;
}

// "Static" vs "Static 3" / "Thermal FEA" vs "Thermal FEA 2": the title already names the type.
bool titleRepeatsType(const char* title, const char* type) {
    if (!title || !type || !type[0]) return false;
    size_t i = 0;
    for (; type[i]; ++i) {
        if (!title[i]) return false;
        const char a = title[i], b = type[i];
        const char la = (a >= 'A' && a <= 'Z') ? (char)(a - 'A' + 'a') : a;
        const char lb = (b >= 'A' && b <= 'Z') ? (char)(b - 'A' + 'a') : b;
        if (la != lb) return false;
    }
    return title[i] == '\0' || title[i] == ' ';
}

// One scaffold frame per live panelBegin().
struct PanelFrame {
    ImGuiStorage*   store      = nullptr;   // HOST window storage (survives the body child)
    ImGuiID         footKey    = 0;
    float           beginY     = 0.0f;      // cursor Y at panelBegin (nothing pinned => no rule)
    float           footStartY = 0.0f;
    float           savedMaxY  = 0.0f;      // host CursorMaxPos.y before the footer
    float           laneW      = 0.0f;      // scrollbar lane reserved in the body this frame
    bool            footer     = false;
    bool            inBody     = false;
    bool            pagePushed = false;
    bool            hasSurf    = false;
    AdaptiveSurface surf;
};
std::vector<PanelFrame> g_panels;

}  // namespace

// ==================================================================================================
// panel header
// ==================================================================================================
bool panelHeader(const char* id, const PanelHeaderSpec& spec) {
    ImGuiContext& g = *GImGui;
    ImGui::PushID(id && id[0] ? id : "kit_panel_header");
    if (spec.actionPressed) *spec.actionPressed = -1;

    ImDrawList*       dl     = ImGui::GetWindowDrawList();
    const ImGuiStyle& sty    = ImGui::GetStyle();
    const float       frameH = ImGui::GetFrameHeight();
    const float       iconSz = ui::metrics::iconHeader();
    const float       gap    = ui::metrics::U(1.5f);
    const float       btnGap = ui::metrics::U(1.0f);
    const bool        hasIcon = spec.icon && spec.icon[0];
    const char*       title   = spec.title ? spec.title : "";

    // Title metrics under the title font.
    if (spec.titleFont) ImGui::PushFont(spec.titleFont);
    const float titleFullW = ImGui::CalcTextSize(title).x;
    const float titleH     = ImGui::GetTextLineHeight();
    // The title never elides below ~8 em or 60% of itself while a status chip is still shown.
    const float minTitleW  = ImMin(titleFullW,
                                   ImMax(ImGui::CalcTextSize("...").x + ImGui::GetFontSize() * kMinTitleEm,
                                         titleFullW * kMinTitleFrac));
    if (spec.titleFont) ImGui::PopFont();

    const float  rowH = ImMax(ImMax(frameH, titleH), hasIcon ? iconSz : 0.0f);
    const ImVec2 p0   = ImGui::GetCursorScreenPos();
    const float  W    = ImMax(ImGui::GetContentRegionAvail().x, ui::metrics::stroke(1.0f));
    const float  right = p0.x + W;

    // Right cluster first: it is never the thing that gives way.
    const char* kDone  = "Done";
    const float doneW  = spec.showDone ? ImGui::CalcTextSize(kDone).x + sty.FramePadding.x * 2.0f
                                       : 0.0f;
    const int   nAct   = (spec.actions && spec.actionCount > 0) ? spec.actionCount : 0;
    // An action with a label is a compact TEXT chip button ("SI", "US"); otherwise an icon button.
    const float chipPadX = ui::metrics::U(1.5f);
    auto actW = [&](const HeaderAction& a) {
        return (a.label && a.label[0]) ? ImGui::CalcTextSize(a.label).x + chipPadX * 2.0f : frameH;
    };
    float       clusterW = doneW;
    for (int i = 0; i < nAct; ++i) clusterW += actW(spec.actions[i]) + (clusterW > 0.0f ? btnGap : 0.0f);
    const float clusterX = right - clusterW;

    float x = p0.x;
    if (hasIcon && paintIcon(dl, spec.icon, ImVec2(x + iconSz * 0.5f, p0.y + rowH * 0.5f), iconSz,
                             role(palette.accent))) {
        x += iconSz + gap;
    }

    // Priority: title > status chip > type chip. The type chip is dropped as soon as the FULL
    // title would not fit beside it (and whenever the title already starts with the type, e.g.
    // "Static 3"); the status chip survives until the title would shrink below minTitleW; only
    // then does the title elide.
    const bool  wantType   = spec.typeChip && spec.typeChip[0] && !titleRepeatsType(title, spec.typeChip);
    const bool  wantStatus = spec.statusChip && spec.statusChip[0];
    const float typeW      = wantType ? chipW(spec.typeChip, false) : 0.0f;
    const float statusW    = wantStatus ? chipW(spec.statusChip, true) : 0.0f;
    const float leftAvail  = ImMax(clusterX - (clusterW > 0.0f ? gap : 0.0f) - x, 0.0f);
    bool showType = wantType, showStatus = wantStatus;
    auto chipsW = [&]() {
        return (showType ? typeW + gap : 0.0f) + (showStatus ? statusW + gap : 0.0f);
    };
    if (showType && titleFullW + chipsW() > leftAvail) showType = false;
    if (showStatus && minTitleW + chipsW() > leftAvail) showStatus = false;
    const float titleW = ImMax(ImMin(titleFullW, leftAvail - chipsW()), 0.0f);

    if (spec.titleFont) ImGui::PushFont(spec.titleFont);
    const std::string shown = elide(title, titleW);
    const ImVec2      ts    = ImGui::CalcTextSize(shown.c_str());
    const ImVec2      tpos(x, p0.y + (rowH - titleH) * 0.5f);
    dl->AddText(tpos, role(palette.text), shown.c_str());
    if (spec.titleFont) ImGui::PopFont();
    if (ts.x > 0.0f) {
        ImGui::SetCursorScreenPos(tpos);
        ImGui::Dummy(ImVec2(ts.x, titleH));
        if (shown != title && ImGui::IsItemHovered()) tooltip(title);
    }
    x += ts.x + gap;

    const float cy = p0.y + (rowH - chipH()) * 0.5f;
    if (showType) {
        ImGui::SetCursorScreenPos(ImVec2(x, cy));
        chip(spec.typeChip, Tone::Neutral);
        x += typeW + gap;
    }
    if (showStatus) {
        ImGui::SetCursorScreenPos(ImVec2(x, cy));
        chip(spec.statusChip, spec.statusTone, /*dot=*/true, spec.statusTip);
    }

    float bx = clusterX;
    const float by = p0.y + (rowH - frameH) * 0.5f;
    for (int i = 0; i < nAct; ++i) {
        const HeaderAction& a  = spec.actions[i];
        const float         aw = actW(a);
        ImGui::PushID(i);
        ImGui::SetCursorScreenPos(ImVec2(bx, by));
        bool hit = false;
        if (a.label && a.label[0]) {
            // Text chip: chip-styled frame-height button; a non-Neutral tone (Accent = override)
            // tints fill, border and text so the state reads without hovering.
            if (!a.enabled) ImGui::BeginDisabled();
            hit = ImGui::InvisibleButton(a.id && a.id[0] ? a.id : "act", ImVec2(aw, frameH));
            IMGUI_TEST_ENGINE_ITEM_INFO(ImGui::GetItemID(), a.label, GImGui->LastItemData.StatusFlags);
            if (!a.enabled) ImGui::EndDisabled();
            const bool  hov  = ImGui::IsItemHovered(ImGuiHoveredFlags_AllowWhenDisabled);
            const float hv   = anim::toward(ImGui::GetID("##hv"), (hov && a.enabled) ? 1.0f : 0.0f,
                                            anim::kFast);
            const bool  tone = a.tone != Tone::Neutral;
            const ImVec2 c0(bx, by), c1(bx + aw, by + frameH);
            const float  rnd = chipRound();
            if (tone) dl->AddRectFilled(c0, c1, toneU32(a.tone, kChipFillA), rnd);
            if (hv > kSettled) dl->AddRectFilled(c0, c1, role(palette.accent, kSegHoverA * hv), rnd);
            dl->AddRect(c0, c1, tone ? toneU32(a.tone, kChipBorderA) : role(palette.border), rnd, 0,
                        ui::metrics::stroke(1.0f));
            const ImVec2 ls = ImGui::CalcTextSize(a.label);
            const ImU32  ink = !a.enabled ? role(palette.textDisabled)
                             : tone       ? toneU32(a.tone)
                                          : role(palette.text);
            dl->AddText(ImVec2(bx + (aw - ls.x) * 0.5f, by + (frameH - ls.y) * 0.5f), ink, a.label);
            if (hov) tooltip(a.tooltip);
            hit = hit && a.enabled;
        } else {
            hit = iconButton(a.id && a.id[0] ? a.id : "act", a.icon, a.tooltip, a.enabled, a.tone, frameH);
        }
        if (hit && spec.actionPressed) *spec.actionPressed = i;
        ImGui::PopID();
        bx += aw + btnGap;
    }

    bool done = false;
    if (spec.showDone) {
        ImGui::SetCursorScreenPos(ImVec2(bx, by));
        done = secondaryButton("Done###kit_panel_done", true, nullptr, ImVec2(doneW, frameH));
        itemTooltip(spec.doneTip);
    }

    ImGui::SetCursorScreenPos(p0);
    ImGui::Dummy(ImVec2(W, rowH));
    ImGui::Dummy(ImVec2(0.0f, headGap()));
    ImGui::PopID();
    return done;
}

// ==================================================================================================
// form tabs
// ==================================================================================================
bool formTabs(const char* id, const char* const* fullLabels, const char* const* shortLabels,
              int count, int* selected, const char* const* tooltips) {
    if (!fullLabels || count <= 0 || !selected) return false;
    ImGui::PushID(id && id[0] ? id : "kit_form_tabs");

    const float avail = ImMax(ImGui::GetContentRegionAvail().x, ui::metrics::stroke(1.0f));
    const float pX    = padX();
    auto widest = [&](const char* const* labels) {
        float w = 0.0f;
        for (int i = 0; i < count; ++i)
            w = ImMax(w, ImGui::CalcTextSize(labels[i] ? labels[i] : "").x);
        return w + pX * 2.0f;
    };
    const char* const* shortSet = shortLabels ? shortLabels : fullLabels;
    const float        wFull    = widest(fullLabels);
    const float        wShort   = widest(shortSet);

    // Full on one row, else short on one row, else the fewest equal rows that fit (full labels
    // preferred at each row count). Labels only elide in the degenerate one-per-row case.
    const char* const* shown  = shortSet;
    int                perRow = 1;
    if (wFull * (float)count <= avail) {
        shown = fullLabels; perRow = count;
    } else if (wShort * (float)count <= avail) {
        shown = shortSet; perRow = count;
    } else {
        for (int rows = 2; rows <= count; ++rows) {
            const int per = (count + rows - 1) / rows;
            if (wFull * (float)per <= avail)  { shown = fullLabels; perRow = per; break; }
            if (wShort * (float)per <= avail) { shown = shortSet;   perRow = per; break; }
        }
    }
    const int   rows  = (count + perRow - 1) / perRow;
    const float h     = ImGui::GetFrameHeight();
    const float vgap  = rowGap();
    const float inset = ui::metrics::stroke(2.0f);
    const float rnd   = chipRound();
    const int   sel   = *selected;

    ImDrawList*  dl     = ImGui::GetWindowDrawList();
    const ImVec2 p0     = ImGui::GetCursorScreenPos();
    const ImVec4 selInk = ui::maxContrastTextOn(roleV(palette.accent, kSegSelA));
    bool         changed = false;

    for (int r = 0; r < rows; ++r) {
        const int    first = r * perRow;
        const int    n     = ImMin(perRow, count - first);
        // Every row spans the full width: a shorter last row ("BOM | To Piping") gets wider
        // cells instead of stopping two-thirds across.
        const float  cellW = avail / (float)n;
        const ImVec2 t0(p0.x, p0.y + (h + vgap) * (float)r);
        const ImVec2 t1(t0.x + avail, t0.y + h);
        dl->AddRectFilled(t0, t1, role(palette.frameBg, kTrackA), rnd);
        dl->AddRect(t0, t1, role(palette.border), rnd, 0, ui::metrics::stroke(1.0f));

        for (int k = 0; k < n; ++k) {
            const int    i = first + k;
            const ImVec2 sMin(t0.x + cellW * (float)k, t0.y);
            const ImVec2 sMax(sMin.x + cellW, t1.y);

            ImGui::PushID(i);
            ImGui::SetCursorScreenPos(sMin);
            if (ImGui::InvisibleButton("##tab", ImVec2(cellW, h)) && *selected != i) {
                *selected = i;
                changed   = true;
            }
            const bool  hovered = ImGui::IsItemHovered();
            const float on = anim::toward(ImGui::GetID("##on"), (*selected == i) ? 1.0f : 0.0f,
                                          anim::kBase);
            const float hv = anim::toward(ImGui::GetID("##hv"), hovered ? 1.0f : 0.0f, anim::kFast);
            ImGui::PopID();

            if (on > kSettled) {
                dl->AddRectFilled(ImVec2(sMin.x + inset, sMin.y + inset),
                                  ImVec2(sMax.x - inset, sMax.y - inset),
                                  role(palette.accent, kSegSelA * on), rnd);
            }
            if (hv > kSettled && on < 1.0f) {
                dl->AddRectFilled(ImVec2(sMin.x + inset, sMin.y + inset),
                                  ImVec2(sMax.x - inset, sMax.y - inset),
                                  role(palette.accent, kSegHoverA * hv * (1.0f - on)), rnd);
            } else if (k > 0 && on <= kSettled && i - 1 != sel) {
                dl->AddLine(ImVec2(sMin.x, sMin.y + inset * 2.0f), ImVec2(sMin.x, sMax.y - inset * 2.0f),
                            role(palette.border), ui::metrics::stroke(1.0f));
            }

            const char*       full = fullLabels[i] ? fullLabels[i] : "";
            const std::string lab  = elide(shown[i] ? shown[i] : full, cellW - inset * 2.0f);
            const ImVec2      ls   = ImGui::CalcTextSize(lab.c_str());
            const ImVec4      idle = roleV(hovered ? palette.text : palette.textDim);
            dl->PushClipRect(ImVec2(sMin.x + inset, sMin.y), ImVec2(sMax.x - inset, sMax.y), true);
            dl->AddText(ImVec2(sMin.x + (cellW - ls.x) * 0.5f, sMin.y + (h - ls.y) * 0.5f),
                        u32(lerpV(idle, selInk, on)), lab.c_str());
            dl->PopClipRect();

            if (hovered) {
                if (tooltips && tooltips[i] && tooltips[i][0]) tooltip(tooltips[i]);
                else if (lab != full)                          tooltip(full);
            }
        }
    }

    ImGui::SetCursorScreenPos(p0);
    ImGui::Dummy(ImVec2(avail, h * (float)rows + vgap * (float)(rows - 1)));
    ImGui::PopID();
    return changed;
}

// ==================================================================================================
// stepper
// ==================================================================================================
bool stepper(const char* id, const StepItem* steps, int count, int* current,
             const StepItem* substeps, int subCount, int* currentSub, int subOf) {
    if (!steps || count <= 0 || !current) return false;
    ImGui::PushID(id && id[0] ? id : "kit_stepper");

    ImDrawList*           dl    = ImGui::GetWindowDrawList();
    const AdaptiveSurface surf  = inkSurf();
    const float           avail = ImMax(ImGui::GetContentRegionAvail().x, ui::metrics::stroke(1.0f));
    const float           txtH  = ImGui::GetTextLineHeight();
    // Compact track: one text line plus a U(1) pad each side (shorter than a frame) — the
    // pinned chrome must leave the scroll body most of the pane.
    const float           h     = txtH + ui::metrics::U(1.0f) * 2.0f;
    const float           segW  = avail / (float)count;
    const float           gr    = txtH * kGlyphFrac;
    const float           gap   = ui::metrics::U(1.0f);
    const float           rnd   = chipRound();
    const float           inset = ui::metrics::U(0.75f);   // wash/underline stay off the track edge
    const ImVec2          p0    = ImGui::GetCursorScreenPos();
    const int             cur   = *current;
    bool                  changed = false;

    dl->AddRectFilled(p0, ImVec2(p0.x + avail, p0.y + h), role(palette.frameBg, kTrackA), rnd);
    dl->AddRect(p0, ImVec2(p0.x + avail, p0.y + h), role(palette.border), rnd, 0,
                ui::metrics::stroke(1.0f));

    for (int i = 0; i < count; ++i) {
        const StepItem& s = steps[i];
        const ImVec2    sMin(p0.x + segW * (float)i, p0.y);
        const ImVec2    sMax(sMin.x + segW, p0.y + h);

        ImGui::PushID(i);
        ImGui::SetCursorScreenPos(sMin);
        if (!s.enabled) ImGui::BeginDisabled();
        const bool pressed = ImGui::InvisibleButton("##step", ImVec2(segW, h));
        if (!s.enabled) ImGui::EndDisabled();
        const bool  hovered = ImGui::IsItemHovered(ImGuiHoveredFlags_AllowWhenDisabled);
        const float on = anim::toward(ImGui::GetID("##on"), cur == i ? 1.0f : 0.0f, anim::kBase);
        const float hv = anim::toward(ImGui::GetID("##hv"), (hovered && s.enabled) ? 1.0f : 0.0f,
                                      anim::kFast);
        ImGui::PopID();
        if (pressed && s.enabled && (*current != i || (currentSub && *currentSub != -1))) {
            *current = i;
            if (currentSub) *currentSub = -1;
            changed = true;
        }

        // Label group (status mark + label), centred in the segment between the chevrons.
        const float       chevW  = txtH * 0.5f;   // room kept clear around each boundary chevron
        const float       labMax = ImMax(segW - chevW * 2.0f - txtH - gap, 0.0f);
        const std::string lab    = elide(s.label ? s.label : "", labMax);
        const float       lw     = ImGui::CalcTextSize(lab.c_str()).x;
        const float       groupW = txtH + (lw > 0.0f ? gap + lw : 0.0f);
        const float       gx     = sMin.x + ImMax((segW - groupW) * 0.5f, chevW);
        const float       ty     = sMin.y + (h - txtH) * 0.5f;

        // Current step: accent wash inset from the track (never touching its border), an accent
        // underline under the label group only, and an accent label. The status mark keeps its
        // own tone so "Results: failed" stays red while selected.
        const float wash = kStepSelA * on + kSegHoverA * hv * (1.0f - on);
        const float wx0  = sMin.x + (i == 0 ? inset : chevW);
        const float wx1  = sMax.x - (i + 1 == count ? inset : chevW);
        if (wash > kSettled && wx1 > wx0) {
            dl->AddRectFilled(ImVec2(wx0, sMin.y + inset), ImVec2(wx1, sMax.y - inset),
                              role(palette.accent, wash), rnd);
        }
        if (on > kSettled) {
            const float bh = ui::metrics::stroke(2.0f);
            const float ux0 = ImMax(gx - gap, wx0 + inset);
            const float ux1 = ImMin(gx + groupW + gap, wx1 - inset);
            if (ux1 > ux0)
                dl->AddRectFilled(ImVec2(ux0, sMax.y - inset - bh), ImVec2(ux1, sMax.y - inset),
                                  role(palette.accent, on), bh * 0.5f);
        }

        statusGlyph(dl, ImVec2(gx + txtH * 0.5f, ty + txtH * 0.5f), gr, s.status, surf);
        const ImVec4 ink = !s.enabled ? roleV(palette.textDisabled)
                                      : lerpV(roleV(hovered ? palette.text : palette.textDim),
                                              roleV(palette.accent), on);
        dl->AddText(ImVec2(gx + txtH + gap, ty), u32(ink), lab.c_str());

        if (hovered) stepTooltip(s);
    }
    // Boundary chevrons last so no wash paints over them; centred on the label line.
    for (int i = 1; i < count; ++i)
        paintChevron(dl, ImVec2(p0.x + segW * (float)i, p0.y + h * 0.5f), txtH * 0.22f,
                     role(palette.textDim));

    float bottom = p0.y + h;
    if (substeps && subCount > 0) {
        // Natural chip widths on ONE row when they fit. Otherwise the chips WRAP onto the fewest
        // rows that hold them, never eliding: an equal-width grid (ceil(n / rows) chips per row,
        // columns aligned like formTabs) when every chip fits its cell, else rows of natural
        // widths with the same per-row count (balanced: 3 + 2, never 4 + a lone chip). Only a
        // single chip wider than the whole pane elides (its tooltip carries the full label).
        const float cH    = chipH();
        const float vgap  = rowGap();
        const float dotR  = ui::metrics::U(0.75f);
        const float cpX   = ui::metrics::U(1.5f);
        const float cgap  = ui::metrics::U(1.0f);
        std::vector<float> nat((size_t)subCount);
        float total = cgap * (float)(subCount - 1), widest = 0.0f;
        for (int j = 0; j < subCount; ++j) {
            nat[(size_t)j] = chipW(substeps[j].label, true);
            total += nat[(size_t)j];
            widest = ImMax(widest, nat[(size_t)j]);
        }
        int   per  = subCount;   // chips per row
        bool  grid = false;
        float cell = 0.0f;
        if (total > avail) {
            per = 1;
            for (int rows = 2; rows <= subCount; ++rows) {
                const int n = (subCount + rows - 1) / rows;
                cell = (avail - cgap * (float)(n - 1)) / (float)n;
                if (widest <= cell) { per = n; grid = true; break; }
                bool fits = true;
                for (int first = 0; first < subCount && fits; first += n) {
                    float rowW = cgap * (float)(ImMin(n, subCount - first) - 1);
                    for (int j = first; j < ImMin(first + n, subCount); ++j) rowW += nat[(size_t)j];
                    fits = rowW <= avail;
                }
                if (fits) { per = n; break; }
            }
        }

        const float y0 = p0.y + h + vgap;
        for (int j = 0; j < subCount; ++j) {
            const StepItem& s   = substeps[j];
            const char*     lbl = s.label ? s.label : "";
            const int       row = j / per, colI = j % per;
            float x = p0.x;
            if (grid) {
                x += (cell + cgap) * (float)colI;
            } else {
                for (int k = row * per; k < j; ++k) x += nat[(size_t)k] + cgap;
            }
            const float  cw = grid ? cell : ImMin(nat[(size_t)j], avail);
            const float  y  = y0 + (cH + vgap) * (float)row;
            const ImVec2 c0(x, y), c1(x + cw, y + cH);

            ImGui::PushID(1000 + j);   // disjoint from the step ids above
            ImGui::SetCursorScreenPos(c0);
            if (!s.enabled) ImGui::BeginDisabled();
            const bool pressed = ImGui::InvisibleButton("##sub", ImVec2(cw, cH));
            if (!s.enabled) ImGui::EndDisabled();
            const bool  hovered = ImGui::IsItemHovered(ImGuiHoveredFlags_AllowWhenDisabled);
            const bool  isSel   = cur == subOf && currentSub && *currentSub == j;
            const float on = anim::toward(ImGui::GetID("##on"), isSel ? 1.0f : 0.0f, anim::kFast);
            const float hv = anim::toward(ImGui::GetID("##hv"), (hovered && s.enabled) ? 1.0f : 0.0f,
                                          anim::kFast);
            ImGui::PopID();
            if (pressed && s.enabled && !isSel) {
                *current = subOf;
                if (currentSub) *currentSub = j;
                changed = true;
            }

            const float r = chipRound();
            dl->AddRectFilled(c0, c1, role(palette.accent, kChipFillA * on + kSegHoverA * hv * (1.0f - on)),
                              r);
            dl->AddRect(c0, c1,
                        u32(lerpV(roleV(palette.border), roleV(palette.accent, kChipBorderA +
                                                                   (1.0f - kChipBorderA) * on), on)),
                        r, 0, ui::metrics::stroke(1.0f));
            // Grid cells centre the dot + label group; natural chips are exactly their content.
            const float cx      = c0.x + ImMax((cw - nat[(size_t)j]) * 0.5f, 0.0f);
            const float textMax = ImMax(cw - cpX * 2.0f - dotR * 2.0f - cgap, 0.0f);
            const std::string lab = elide(lbl, textMax);
            paintStatusDot(dl, ImVec2(cx + cpX + dotR, c0.y + cH * 0.5f), dotR, s.status);
            if (!lab.empty()) {
                const ImVec4 ink = !s.enabled ? roleV(palette.textDisabled)
                                              : lerpV(roleV(palette.text), roleV(palette.accent), on);
                dl->AddText(ImVec2(cx + cpX + dotR * 2.0f + cgap, c0.y + (cH - txtH) * 0.5f),
                            u32(ink), lab.c_str());
            }
            if (hovered) stepTooltip(s);
        }
        const int rows = (subCount + per - 1) / per;
        bottom = y0 + cH * (float)rows + vgap * (float)(rows - 1);
    }

    ImGui::SetCursorScreenPos(p0);
    ImGui::Dummy(ImVec2(avail, bottom - p0.y));
    ImGui::PopID();
    return changed;
}

float stepperCompactWidth(const StepItem* steps, int count, bool withLabels) {
    if (!steps || count <= 0) return 0.0f;
    const float txtH = ImGui::GetTextLineHeight();
    const float gap  = ui::metrics::U(1.0f);
    const float sep  = ui::metrics::U(1.5f);
    float       w    = 0.0f;
    for (int i = 0; i < count; ++i) {
        w += txtH;
        if (withLabels && steps[i].label && steps[i].label[0])
            w += gap + ImGui::CalcTextSize(steps[i].label).x;
        if (i + 1 < count) w += sep;
    }
    return w;
}

bool stepperCompact(const char* id, const StepItem* steps, int count, int* current) {
    if (!steps || count <= 0 || !current) return false;
    ImGui::PushID(id && id[0] ? id : "kit_stepper_compact");

    ImDrawList*           dl     = ImGui::GetWindowDrawList();
    const AdaptiveSurface surf   = inkSurf();
    const float           txtH   = ImGui::GetTextLineHeight();
    const float           gap    = ui::metrics::U(1.0f);
    const float           sep    = ui::metrics::U(1.5f);
    const float           avail  = ImMax(ImGui::GetContentRegionAvail().x, ui::metrics::stroke(1.0f));
    const bool            labels = stepperCompactWidth(steps, count, true) <= avail;
    const ImVec2          p0     = ImGui::GetCursorScreenPos();
    float                 x      = p0.x;
    bool                  changed = false;

    for (int i = 0; i < count; ++i) {
        const StepItem& s   = steps[i];
        const char*     lbl = s.label ? s.label : "";
        const float     lw  = (labels && lbl[0]) ? ImGui::CalcTextSize(lbl).x : 0.0f;
        const float     w   = txtH + (lw > 0.0f ? gap + lw : 0.0f);

        ImGui::PushID(i);
        ImGui::SetCursorScreenPos(ImVec2(x, p0.y));
        if (!s.enabled) ImGui::BeginDisabled();
        const bool pressed = ImGui::InvisibleButton("##cstep", ImVec2(w, txtH));
        if (!s.enabled) ImGui::EndDisabled();
        const bool hovered = ImGui::IsItemHovered(ImGuiHoveredFlags_AllowWhenDisabled);
        ImGui::PopID();
        if (pressed && s.enabled && *current != i) {
            *current = i;
            changed  = true;
        }

        statusGlyph(dl, ImVec2(x + txtH * 0.5f, p0.y + txtH * 0.5f), txtH * kGlyphFrac, s.status, surf);
        if (lw > 0.0f) {
            const float* ink = !s.enabled ? palette.textDisabled
                             : (*current == i) ? palette.accent
                             : hovered ? palette.text : palette.textDim;
            dl->AddText(ImVec2(x + txtH + gap, p0.y), role(ink), lbl);
        }
        if (hovered) {
            if (s.tooltip && s.tooltip[0]) tooltip(s.tooltip);
            else if (lw <= 0.0f && lbl[0]) tooltip(lbl);
        }
        x += w + sep;
    }

    ImGui::SetCursorScreenPos(p0);
    ImGui::Dummy(ImVec2(ImMax(x - sep - p0.x, ui::metrics::stroke(1.0f)), txtH));
    ImGui::PopID();
    return changed;
}

// ==================================================================================================
// panel scaffold
// ==================================================================================================
void panelBegin(const char* id, const AdaptiveSurface* surf) {
    ImGui::PushID(id && id[0] ? id : "kit_panel");
    PanelFrame f;
    f.store   = ImGui::GetStateStorage();
    f.footKey = ImGui::GetID("##kit_panel_footh");
    f.beginY  = ImGui::GetCursorScreenPos().y;
    if (surf) {
        f.hasSurf = true;
        f.surf    = *surf;
    }
    g_panels.push_back(f);
}

void panelBodyBegin(const char* page) {
    if (g_panels.empty()) return;   // unmatched: refuse rather than corrupt the ImGui stacks
    PanelFrame& f = g_panels.back();
    if (f.inBody) return;
    // The hairline separates PINNED chrome from the body; with nothing pinned above (header-less
    // dialogs whose title bar is the header) it would only be a stray band + rule.
    if (std::fabs(ImGui::GetCursorScreenPos().y - f.beginY) > ui::metrics::stroke(1.0f) * 0.5f) rule();
    // The footer measured itself last frame; the body takes exactly what is left above it (the
    // ItemSpacing EndChild adds below the child included).
    const float footH = f.store->GetFloat(f.footKey, 0.0f);
    const float sp    = ImGui::GetStyle().ItemSpacing.y;
    const float bodyH = ImMax(ImGui::GetContentRegionAvail().y - (footH > 0.0f ? footH + sp : 0.0f),
                              ImGui::GetFrameHeight() * 2.0f);
    f.pagePushed = page && page[0];
    if (f.pagePushed) ImGui::PushID(page);
    // Scrollbar lane: the right kChatScrollbarLane is ALWAYS reserved for the pill, so it never
    // paints under/over cards and the content width never jumps by a lane when a page starts or
    // stops overflowing (cards, combos and the unit lane keep one width per pane). ImGui's
    // WindowPadding is symmetric, so pad both sides by the lane and pull the left edge back with
    // a negative Indent (the copilot/ai_workspace pattern): content keeps its left edge and ends
    // a lane short of the right edge (avail, wrapping and child widths all honour it).
    f.laneW = ui::metrics::px(ui::kChatScrollbarLane);
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(f.laneW, 0.0f));
    ImGui::BeginChild("##kit_panel_body", ImVec2(0.0f, bodyH), ImGuiChildFlags_AlwaysUseWindowPadding,
                      ImGuiWindowFlags_NoScrollbar);
    ImGui::PopStyleVar();
    ImGui::Indent(-f.laneW);
    f.inBody = true;
}

namespace {
// Scroll-edge cues over the body rect `r` (lane excluded): a fade into the pane surface at each
// edge with content beyond it, plus a small down chevron while more content sits below. Drawn in
// a NoInputs overlay child created after the body, so it paints ABOVE the body's card children
// (child windows render after their parent's draw list) and never takes a click or the wheel.
// Layout-neutral: the host's cursor and content size are restored afterwards.
void bodyEdgeCues(const ImRect& r, bool above, bool below, const ImVec4& surface) {
    if ((!above && !below) || r.GetWidth() <= 0.0f || r.GetHeight() <= 0.0f) return;
    const LayoutSnap snap;
    ImGui::SetCursorScreenPos(r.Min);
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(0.0f, 0.0f));
    ImGui::BeginChild("##kit_panel_cues", r.GetSize(), ImGuiChildFlags_None,
                      ImGuiWindowFlags_NoInputs | ImGuiWindowFlags_NoBackground |
                          ImGuiWindowFlags_NoScrollbar | ImGuiWindowFlags_NoScrollWithMouse |
                          ImGuiWindowFlags_NoSavedSettings);
    ImGui::PopStyleVar();
    ImDrawList*  dl    = ImGui::GetWindowDrawList();
    const ImU32  clear = u32(ImVec4(surface.x, surface.y, surface.z, 0.0f));
    const ImU32  solid = u32(surface);
    if (above) {
        const float fh = ImMin(ui::metrics::U(kFadeTopU), r.GetHeight() * 0.5f);
        dl->AddRectFilledMultiColor(r.Min, ImVec2(r.Max.x, r.Min.y + fh), solid, solid, clear, clear);
    }
    if (below) {
        const float fh = ImMin(ui::metrics::U(kFadeBottomU), r.GetHeight() * 0.5f);
        dl->AddRectFilledMultiColor(ImVec2(r.Min.x, r.Max.y - fh), r.Max, clear, clear, solid, solid);
        const float cr = ui::metrics::U(kFadeChevronU);
        paintChevronDown(dl, ImVec2((r.Min.x + r.Max.x) * 0.5f, r.Max.y - fh * 0.45f), cr,
                         role(palette.textDim));
    }
    ImGui::EndChild();
    snap.restore();
}
}  // namespace

void panelBodyEnd() {
    if (g_panels.empty()) return;
    PanelFrame& f = g_panels.back();
    if (!f.inBody) return;
    ImGui::Indent(f.laneW);   // undo before the scrollbar geometry read
    // Same phantom-overflow tolerance as drawWindowPillScrollbarV: cues exactly when the pill shows.
    ImGuiWindow* body     = ImGui::GetCurrentWindow();
    const float  scrollY  = ImGui::GetScrollY();
    const float  scrollMx = ImGui::GetScrollMaxY();
    const bool   overflow = scrollMx > body->WindowPadding.y + ui::metrics::px(4.0f);
    const float  edge     = ui::metrics::stroke(1.0f);
    const bool   above    = overflow && scrollY > edge;
    const bool   below    = overflow && scrollY < scrollMx - edge;
    const ImRect cueRect(body->Pos, ImVec2(body->Pos.x + body->Size.x - f.laneW,
                                           body->Pos.y + body->Size.y));
    const AdaptiveSurface surf = f.hasSurf ? f.surf : inkSurf();
    ui::drawWindowPillScrollbarV(surf, ui::kChatScrollbar);
    ImGui::EndChild();
    // Fade into the pane's own surface (its adaptive tint when the host passed one).
    const ImVec4 surface = (f.hasSurf && f.surf.tint.w > 0.0f) ? f.surf.tint : roleV(palette.glassBg);
    bodyEdgeCues(cueRect, above, below, surface);
    if (f.pagePushed) ImGui::PopID();
    f.pagePushed = false;
    f.inBody     = false;
    f.laneW      = 0.0f;
}

void panelFooterBegin() {
    if (g_panels.empty()) return;
    PanelFrame& f = g_panels.back();
    if (f.inBody) panelBodyEnd();
    f.footer     = true;
    f.footStartY = ImGui::GetCursorScreenPos().y;
    // Measure the footer in isolation: anything earlier in the host window that reached further
    // down (CursorMaxPos is a window-wide max) must not inflate its height.
    ImGuiWindow* win = ImGui::GetCurrentWindow();
    f.savedMaxY = win->DC.CursorMaxPos.y;
    win->DC.CursorMaxPos.y = f.footStartY;
    rule();
}

void panelEnd() {
    if (g_panels.empty()) return;
    if (g_panels.back().inBody) panelBodyEnd();
    const PanelFrame f = g_panels.back();
    g_panels.pop_back();
    // CursorMaxPos excludes the trailing ItemSpacing, so the footer lands flush with the bottom.
    float footH = 0.0f;
    if (f.footer) {
        ImGuiWindow* win = ImGui::GetCurrentWindow();
        footH = ImMax(win->DC.CursorMaxPos.y - f.footStartY, 0.0f);
        win->DC.CursorMaxPos.y = ImMax(win->DC.CursorMaxPos.y, f.savedMaxY);
    }
    if (f.store) {
        // A footer that grew/shrank this frame was laid out against the old height; pump one
        // more frame so the body re-fits immediately instead of at the 20 Hz idle floor. The
        // footer status is capped at two lines, so this happens on state/message changes only.
        if (std::fabs(f.store->GetFloat(f.footKey, 0.0f) - footH) > ui::metrics::stroke(1.0f) * 0.5f)
            anim::requestFrames();
        f.store->SetFloat(f.footKey, footH);
    }
    ImGui::PopID();
}

// ==================================================================================================
// action footer
// ==================================================================================================
namespace {

// Status of at most kStatusLines lines: optional tone dot (on the first line) + text word-wrapped
// to the available width; only text beyond the last line elides, and then the full text shows on
// hover. Messages are written action-first, so the actionable part is never the part cut.
void statusLines(const char* msg, ImU32 textCol, ImU32 dotCol) {
    if (!msg || !msg[0]) return;
    ImDrawList*  dl   = ImGui::GetWindowDrawList();
    const float  txtH = ImGui::GetTextLineHeight();
    const float  dotR = ui::metrics::U(0.75f);
    const float  dgap = ui::metrics::U(1.0f);
    const float  W    = ImMax(ImGui::GetContentRegionAvail().x, ui::metrics::stroke(1.0f));
    const ImVec2 p    = ImGui::GetCursorScreenPos();
    const float  tx   = dotCol ? dotR * 2.0f + dgap : 0.0f;
    bool         cut  = false;
    const std::vector<std::string> lines =
        detail::wrapLines(msg, ImMax(W - tx, ui::metrics::stroke(1.0f)), kStatusLines, &cut);
    if (lines.empty()) return;
    if (dotCol) dl->AddCircleFilled(ImVec2(p.x + dotR, p.y + txtH * 0.5f), dotR, dotCol);
    for (size_t i = 0; i < lines.size(); ++i)
        dl->AddText(ImVec2(p.x + tx, p.y + txtH * (float)i), textCol, lines[i].c_str());
    ImGui::Dummy(ImVec2(W, txtH * (float)lines.size()));
    if (cut && ImGui::IsItemHovered()) tooltip(msg);
}

// Button label fitted to `w` (frame padding off), "###id" keeps the id stable when it elides.
std::string fitLabel(const char* label, float w, const char* id, bool* elided) {
    const float       inner = ImMax(w - ImGui::GetStyle().FramePadding.x * 2.0f, 0.0f);
    const std::string shown = elide(label ? label : "", inner);
    if (elided) *elided = shown != (label ? label : "");
    return shown + "###" + id;
}

}  // namespace

void footerStatus(const char* msg, Tone t) {
    statusLines(msg, toneU32(t), toneU32(t));
}

FooterAction actionFooter(const char* id, const ActionSpec& a) {
    ImGuiContext& g = *GImGui;
    ImGui::PushID(id && id[0] ? id : "kit_action_footer");
    FooterAction out = FooterAction::None;

    const float btnH = ui::metrics::reBtnH();
    const float W    = ImMax(ImGui::GetContentRegionAvail().x, ui::metrics::stroke(1.0f));
    const float gap  = ImGui::GetStyle().ItemSpacing.x;

    if (a.note && a.note[0]) statusLines(a.note, role(palette.textDim), 0);

    if (a.running) {
        // Caption on its own line (stage left, elapsed right) above a full-width bar. Feeding a
        // per-second "stage · 12 s" string into progressBar() would re-key its eased fill every
        // second, so the bar keeps a stable id and the caption is painted here.
        const char*       stage = (a.stage && a.stage[0]) ? a.stage : "Running";
        const std::string el    = a.elapsed >= 0.0 ? fmtDuration(a.elapsed) : std::string();
        const float       txtH  = ImGui::GetTextLineHeight();
        const ImVec2      cp    = ImGui::GetCursorScreenPos();
        const float       elW   = el.empty() ? 0.0f : ImGui::CalcTextSize(el.c_str()).x;
        const std::string flat  = oneLine(stage);
        const std::string st    = elide(flat.c_str(), W - (elW > 0.0f ? elW + gap : 0.0f));
        ImDrawList*       dl    = ImGui::GetWindowDrawList();
        dl->AddText(cp, role(palette.text), st.c_str());
        if (!el.empty()) dl->AddText(ImVec2(cp.x + W - elW, cp.y), role(palette.textDim), el.c_str());
        ImGui::Dummy(ImVec2(W, txtH));
        if (st != stage && ImGui::IsItemHovered()) tooltip(stage);
        progressBar(a.progress, nullptr);
        if (!a.cancellable) {
            const std::string why = std::string("Cannot cancel: ") +
                                    ((a.cancelBlocker && a.cancelBlocker[0]) ? a.cancelBlocker
                                                                             : "this stage has no cancellation route.");
            footerStatus(why.c_str(), Tone::Neutral);
        }
    } else if (!a.primaryEnabled && a.blocker && a.blocker[0]) {
        footerStatus(a.blocker, a.blockerTone);
    }

    // Button row. Main action = Cancel while running (absent when not cancellable), else the
    // primary. With a secondary: side by side (secondary at content width on the left, main
    // filling the rest) when BOTH labels fit unclipped; otherwise the two stack full-width
    // (secondary above, main at the bottom). Labels elide only if even a full row is too narrow.
    const bool  hasMain = !a.running || a.cancellable;
    const char* mainLab = a.running ? (a.cancelRequested ? "Cancel requested\xe2\x80\xa6" : "Cancel")
                                    : (a.primaryLabel && a.primaryLabel[0] ? a.primaryLabel : "Run");
    auto natW = [&](const char* l) {
        return ImGui::CalcTextSize(l, nullptr, true).x + ImGui::GetStyle().FramePadding.x * 2.0f +
               padX() * 2.0f;
    };
    float mainW = W;
    const bool hasSecondary = a.secondaryLabel && a.secondaryLabel[0];
    if (hasSecondary) {
        const float sNat = natW(a.secondaryLabel);
        const bool  side = !hasMain || sNat + gap + natW(mainLab) <= W;
        const float sw   = side ? ImMin(sNat, W) : W;
        bool        el   = false;
        const std::string lab = fitLabel(a.secondaryLabel, sw, "kit_af_secondary", &el);
        if (secondaryButton(lab.c_str(), a.secondaryEnabled, a.secondaryReason, ImVec2(sw, btnH)))
            out = FooterAction::Secondary;
        IMGUI_TEST_ENGINE_ITEM_INFO(ImGui::GetItemID(), a.secondaryLabel, GImGui->LastItemData.StatusFlags);
        if (el && a.secondaryEnabled) itemTooltip(a.secondaryLabel);
        if (side && hasMain) {
            ImGui::SameLine(0.0f, gap);
            mainW = ImMax(W - sw - gap, ui::metrics::stroke(1.0f));
        }
    }

    if (a.running) {
        if (a.cancellable) {
            const std::string lab = fitLabel(mainLab, mainW, "kit_af_cancel", nullptr);
            if (dangerButton(lab.c_str(), !a.cancelRequested, "Waiting for the solver's next checkpoint.",
                             ImVec2(mainW, btnH)))
                out = FooterAction::Cancel;
            IMGUI_TEST_ENGINE_ITEM_INFO(ImGui::GetItemID(), mainLab, GImGui->LastItemData.StatusFlags);
        }
        // Not cancellable: nothing to press (the status above says why).
    } else {
        bool              el  = false;
        const std::string lab = fitLabel(mainLab, mainW, "kit_af_primary", &el);
        const bool pressed = a.primaryAsSecondary
                                 ? secondaryButton(lab.c_str(), a.primaryEnabled, a.blocker, ImVec2(mainW, btnH))
                                 : primaryButton(lab.c_str(), a.primaryEnabled, a.blocker, ImVec2(mainW, btnH));
        IMGUI_TEST_ENGINE_ITEM_INFO(ImGui::GetItemID(), mainLab, GImGui->LastItemData.StatusFlags);
        if (el && a.primaryEnabled) itemTooltip(mainLab);
        if (pressed) out = FooterAction::Primary;
    }

    ImGui::PopID();
    return out;
}

// ==================================================================================================
// removable card
// ==================================================================================================
bool removableCard(const char* id, const char* title, const char* icon, const float tint[3],
                   const char* removeTip, bool* removed, bool* open) {
    const bool body    = cardBegin(id, title, icon, open, tint);
    const bool hasIcon = icon && icon[0];
    // cardBegin's header row height: one text line, or the header icon tier when it has an icon.
    const float txtH = ImGui::GetTextLineHeight();
    const float rowH = hasIcon ? ImMax(txtH, ui::metrics::iconHeader()) : txtH;
    // The delete button is a full frame-height square (the same hit size as every other icon
    // button; its glyph lands on the header icon tier), right-aligned to the card's content edge
    // (one padX in from the border) and centred on the title row. It overhangs the row into the
    // card's top padding / head gap, so it is placed as an OVERLAY: the layout snapshot keeps it
    // from growing the header line or the card (a collapsed card stays exactly its title row).
    const float     s = ImMax(ImGui::GetFrameHeight(), ui::metrics::iconHeader());
    const LayoutSnap snap;
    cardHeaderRight(s);
    const ImVec2 at = ImGui::GetCursorScreenPos();   // title-row top, right-aligned
    ImGui::SetCursorScreenPos(ImVec2(at.x, at.y + (rowH - s) * 0.5f));
    if (iconButton("##kit_card_rm", "delete", removeTip && removeTip[0] ? removeTip : "Remove", true,
                   Tone::Neutral, s) &&
        removed)
        *removed = true;
    snap.restore();
    return body;
}

// ==================================================================================================
// canvas
// ==================================================================================================
Canvas canvasFrame(const char* id, const ImVec2& size, const char* hint, float keyW) {
    Canvas c;
    ImGui::PushID(id && id[0] ? id : "kit_canvas");
    const ImVec2 avail = ImGui::GetContentRegionAvail();
    ImVec2       sz    = ImGui::CalcItemSize(size, avail.x, avail.y);
    sz.x = ImMax(sz.x, ui::metrics::px(kCanvasMinWDp));
    sz.y = ImMax(sz.y, ui::metrics::px(kCanvasMinHDp));

    const ImVec2 p0 = ImGui::GetCursorScreenPos();
    const ImVec2 p1(p0.x + sz.x, p0.y + sz.y);
    c.id      = ImGui::GetID("##kit_canvas");
    c.clicked = ImGui::InvisibleButton("##kit_canvas", sz);
    c.hovered = ImGui::IsItemHovered();
    c.active  = ImGui::IsItemActive();
    c.dl      = ImGui::GetWindowDrawList();
    c.frame   = ImRect(p0, p1);

    const float rnd = rowRound();
    const float t   = ui::metrics::stroke(1.0f);
    c.dl->AddRectFilled(p0, p1, role(palette.frameBg), rnd);
    c.dl->AddRect(p0, p1, role(palette.border), rnd, 0, t);

    const bool  hasHint = hint && hint[0];
    const float txtH    = ImGui::GetTextLineHeight();
    const float stripH  = (hasHint || keyW > 0.0f) ? txtH + padY() * 2.0f : 0.0f;
    c.strip = ImRect(ImVec2(p0.x, p1.y - stripH), p1);
    c.inner = ImRect(ImVec2(p0.x + t, p0.y + t), ImVec2(p1.x - t, p1.y - stripH - (stripH > 0.0f ? t : 0.0f)));
    c.keyX  = p1.x - padX() - ImMax(keyW, 0.0f);
    if (stripH > 0.0f) {
        c.dl->AddLine(ImVec2(p0.x + t, c.strip.Min.y), ImVec2(p1.x - t, c.strip.Min.y),
                      role(palette.border), t);
        if (hasHint) {
            // The hint elides into the space left of the key; the full hint shows on hover of
            // the strip whenever it was cut.
            const float       hintR = keyW > 0.0f ? c.keyX - padX() : p1.x - padX();
            const std::string flat  = oneLine(hint);
            const std::string h     = elide(flat.c_str(), hintR - (p0.x + padX()));
            c.dl->AddText(ImVec2(p0.x + padX(), c.strip.Min.y + (stripH - txtH) * 0.5f),
                          role(palette.textDim), h.c_str());
            if (h != flat && c.hovered && c.strip.Contains(ImGui::GetIO().MousePos)) tooltip(hint);
        }
    }
    ImGui::PopID();
    return c;
}

bool canvasEmpty(const Canvas& canvas, const char* icon, const char* title, const char* body,
                 const char* actionLabel, bool actionEnabled) {
    if (!canvas.dl || canvas.inner.GetWidth() <= 0.0f || canvas.inner.GetHeight() <= 0.0f)
        return false;
    bool clicked = false;
    ImGui::SetCursorScreenPos(canvas.inner.Min);
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(padX(), 0.0f));
    const ImGuiID cid = ImGui::GetIDWithSeed("##kit_canvas_empty", nullptr, canvas.id);
    if (ImGui::BeginChild(cid, canvas.inner.GetSize(), ImGuiChildFlags_AlwaysUseWindowPadding,
                          ImGuiWindowFlags_NoBackground | ImGuiWindowFlags_NoScrollbar |
                              ImGuiWindowFlags_NoScrollWithMouse)) {
        // Centre vertically on last frame's measured block height.
        ImGuiStorage* store = ImGui::GetStateStorage();
        const ImGuiID hKey  = ImGui::GetID("##kit_canvas_empty_h");
        const float   blockH = store->GetFloat(hKey, 0.0f);
        const float   availH = ImGui::GetContentRegionAvail().y;
        if (blockH > 0.0f && blockH < availH)
            ImGui::SetCursorPosY(ImGui::GetCursorPosY() + (availH - blockH) * 0.5f);
        const float y0 = ImGui::GetCursorScreenPos().y;
        clicked = emptyState(icon, title, body, actionLabel, actionEnabled);
        store->SetFloat(hKey, ImMax(ImGui::GetCursorScreenPos().y - ImGui::GetStyle().ItemSpacing.y - y0, 0.0f));
    }
    ImGui::EndChild();
    ImGui::PopStyleVar();
    // Re-establish the layout exactly as canvasFrame left it (cursor below the frame).
    ImGui::SetCursorScreenPos(canvas.frame.Min);
    ImGui::Dummy(canvas.frame.GetSize());
    return clicked;
}

// ==================================================================================================
// grouped picker
// ==================================================================================================
int groupedPicker(const char* id, const PickerGroup* groups, int groupCount, float maxWidth,
                  float maxHeight) {
    if (!groups || groupCount <= 0) return -1;
    ImGui::PushID(id && id[0] ? id : "kit_grouped_picker");

    const float W      = maxWidth > 0.0f ? maxWidth : ui::metrics::px(kPickerWDp);
    const float txtH   = ImGui::GetTextLineHeight();
    const float pX     = padX();
    const float pY     = padY();
    const float iconSz = txtH;
    const float gap    = ui::metrics::U(1.5f);
    const float lnGap  = ui::metrics::U(0.5f);
    const float cpX    = ui::metrics::U(1.5f);
    const float cpY    = ui::metrics::U(0.5f);
    const float lane   = ui::metrics::px(ui::kChatScrollbarLane);
    ImFont*     font   = ImGui::GetFont();
    const float fsz    = ImGui::GetFontSize();

    // Height cap for the WHOLE containing window, measured from its top: what is already above
    // the list (the caller's title row, window padding) and the bottom padding come off it. The
    // list also never runs past the main viewport's work area (one gutter clear of the status
    // bar), wherever the caller anchored the popup.
    ImGuiWindow*         host    = ImGui::GetCurrentWindow();
    const ImGuiStyle&    sty     = ImGui::GetStyle();
    const ImGuiViewport* vp      = ImGui::GetMainViewport();
    const float          vpCap   = vp->WorkSize.y * kPickerMaxHFrac;
    const float          winCap  = maxHeight > 0.0f ? ImMin(maxHeight, vpCap) : vpCap;
    const float          above   = ImGui::GetCursorScreenPos().y - host->Pos.y;
    const float          roomBelow = vp->WorkPos.y + vp->WorkSize.y - gutter() -
                                     ImGui::GetCursorScreenPos().y - host->WindowPadding.y;
    const float          listCap = ImMax(ImMin(winCap - above - host->WindowPadding.y, roomBelow),
                                         ImGui::GetFrameHeight() * 4.0f);

    // The list measures its own content height (last frame) to size itself: auto-fit to the
    // content while it fits, else the cap plus a reserved pill-scrollbar lane (the content keeps
    // its width W either way).
    ImGuiStorage* store    = ImGui::GetStateStorage();
    const ImGuiID hKey     = ImGui::GetID("##kit_picker_h");
    const float   contentH = store->GetFloat(hKey, 0.0f);
    const bool    scrolls  = contentH <= 0.0f || contentH > listCap;
    const float   listH    = scrolls ? listCap : contentH;
    const float   laneW    = (contentH > listCap) ? lane : 0.0f;

    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(laneW, 0.0f));
    ImGui::BeginChild("##kit_picker_list", ImVec2(W + laneW, listH),
                      ImGuiChildFlags_AlwaysUseWindowPadding,
                      ImGuiWindowFlags_NoScrollbar | ImGuiWindowFlags_NoBackground);
    ImGui::PopStyleVar();
    if (laneW > 0.0f) ImGui::Indent(-laneW);   // content starts flush left; the lane stays right

    int picked = -1;
    int flat   = 0;
    for (int g = 0; g < groupCount; ++g) {
        const PickerGroup& grp = groups[g];
        ImGui::PushID(g);
        if (g > 0) gapRow();   // compact group rhythm (not the full section gap)
        if (grp.title && grp.title[0]) sectionHeader(grp.title, nullptr, nullptr, -1, /*first=*/true);
        for (int i = 0; i < grp.count && grp.items; ++i, ++flat) {
            const PickerItem& it      = grp.items[i];
            const bool        hasDesc = it.desc && it.desc[0];
            const bool        showWhy = !it.enabled && it.reason && it.reason[0];
            const float       textX   = pX + iconSz + gap;           // from the row's left edge
            const float       textW   = ImMax(W - textX - pX, ui::metrics::stroke(1.0f));
            const float       whyTxtW = ImMax(textW - cpX * 2.0f, ui::metrics::stroke(1.0f));
            const ImVec2      whySz   = showWhy ? ImGui::CalcTextSize(it.reason, nullptr, false, whyTxtW)
                                                : ImVec2(0.0f, 0.0f);
            // Descriptions WRAP (up to kPickerDescLines, so every row stays bounded); only text
            // past the last line elides, with the full description on hover.
            bool descCut = false;
            const std::vector<std::string> desc =
                hasDesc ? detail::wrapLines(it.desc, textW, kPickerDescLines, &descCut)
                        : std::vector<std::string>();
            const float       h       = pY * 2.0f + txtH +
                                        (desc.empty() ? 0.0f : lnGap + txtH * (float)desc.size()) +
                                        (showWhy ? lnGap + whySz.y + cpY * 2.0f : 0.0f);

            ImGui::PushID(i);
            if (!it.enabled) ImGui::BeginDisabled();
            const bool hit = rowBegin("row", h, false);
            if (!it.enabled) ImGui::EndDisabled();
            const bool   hovered = ImGui::IsItemHovered(ImGuiHoveredFlags_AllowWhenDisabled);
            const ImVec2 o       = ImGui::GetCursorScreenPos();     // content origin (padded)
            const float  x       = o.x + iconSz + gap;
            const float  dimMul  = it.enabled ? 1.0f : kDisabledA;
            ImDrawList*  dl      = ImGui::GetWindowDrawList();

            paintIcon(dl, it.icon, ImVec2(o.x + iconSz * 0.5f, o.y + txtH * 0.5f), iconSz,
                      role(it.enabled ? palette.accent : palette.textDisabled), dimMul);
            const std::string t = elide(it.title ? it.title : "", textW);
            dl->AddText(ImVec2(x, o.y), role(it.enabled ? palette.text : palette.textDisabled), t.c_str());
            float y = o.y + txtH;
            if (!desc.empty()) {
                y += lnGap;
                for (const std::string& line : desc) {
                    dl->AddText(ImVec2(x, y), role(palette.textDim, dimMul), line.c_str());
                    y += txtH;
                }
            }
            if (showWhy) {
                // Wrapping Warn chip, painted like kit::chip but never truncated.
                y += lnGap;
                const ImVec2 c0(x, y), c1(x + whySz.x + cpX * 2.0f, y + whySz.y + cpY * 2.0f);
                dl->AddRectFilled(c0, c1, toneU32(Tone::Warn, kChipFillA), chipRound());
                dl->AddRect(c0, c1, toneU32(Tone::Warn, kChipBorderA), chipRound(), 0,
                            ui::metrics::stroke(1.0f));
                dl->AddText(font, fsz, ImVec2(c0.x + cpX, c0.y + cpY), toneU32(Tone::Warn), it.reason,
                            nullptr, whyTxtW);
            }
            rowEnd();

            if (hovered) {
                if (descCut)                        tooltip(it.desc);
                else if (it.title && t != it.title) tooltip(it.title);
            }
            if (hit && it.enabled) picked = flat;
            ImGui::PopID();
        }
        ImGui::PopID();
    }

    // Content height (trailing item spacing excluded) for next frame's sizing; pump a frame when
    // it changed so the popup settles at once.
    const float measured = ImMax(ImGui::GetCursorPosY() - sty.ItemSpacing.y, 0.0f);
    if (std::fabs(measured - contentH) > ui::metrics::stroke(1.0f) * 0.5f) anim::requestFrames();
    store->SetFloat(hKey, measured);

    if (laneW > 0.0f) ImGui::Indent(laneW);   // undo before the scrollbar geometry read
    ui::drawWindowPillScrollbarV(inkSurf(), ui::kChatScrollbar);
    ImGui::EndChild();

    if (picked >= 0 && (ImGui::GetCurrentWindow()->Flags & ImGuiWindowFlags_Popup))
        ImGui::CloseCurrentPopup();
    ImGui::PopID();
    return picked;
}

}  // namespace kit
}  // namespace ui
