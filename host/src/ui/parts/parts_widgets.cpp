// Parts Library shared widgets — see parts_widgets.h. GUI V2 phase A: the border-highlight
// selection language (selection = border, not wash) + atlas icons via drawIconGlass.
#include "parts_widgets.h"

#include <algorithm>
#include <array>
#include <cmath>
#include <map>
#include <string>

#include "gl_compat.h"

#include "stb_image.h"

#include "core/app_state.h"
#include "ui/icons.h"
#include "ui/metrics.h"    // ui::metrics::partsPadX — the ONE horizontal inset of this workspace
#include "ui_theme.h"

#include "parts_internal.h"

namespace ui {
namespace parts {

// ---- base64 ------------------------------------------------------------------------

std::vector<unsigned char> b64decode(const std::string& in) {
    static int table[256];
    static bool init = false;
    if (!init) {
        for (int i = 0; i < 256; ++i) table[i] = -1;
        const char* al = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
        for (int i = 0; i < 64; ++i) table[(unsigned char)al[i]] = i;
        init = true;
    }
    std::vector<unsigned char> out;
    out.reserve(in.size() * 3 / 4);
    int val = 0, bits = -8;
    for (unsigned char c : in) {
        int d = table[c];
        if (d == -1) { if (c == '=') break; continue; }
        val = (val << 6) + d;
        bits += 6;
        if (bits >= 0) {
            out.push_back((unsigned char)((val >> bits) & 0xFF));
            bits -= 8;
        }
    }
    return out;
}

// ---- headers / states ----------------------------------------------------------------

void sectionHeader(const char* label, bool firstInPane, const char* icon,
                   const char* trailing) {
    const float sc  = ui::uiScale;
    const float pad = ui::metrics::partsPadX();
    if (!firstInPane) ImGui::Dummy(ImVec2(0, 8.0f * sc));
    // The label sits on the pane's TEXT line (pane gutter + partsPadX) — the same line
    // borderRow paints its icon/label on, a grid card starts its title on, and ImGui puts
    // the text of a framed widget on (partsPadX == style.FramePadding.x). A header and the
    // body under it therefore share one left edge instead of stepping by the row padding.
    ImGui::Indent(pad);
    const ImVec2 rowMin = ImGui::GetCursorScreenPos();
    const float  rowW   = ImGui::GetContentRegionAvail().x;
    ImDrawList*  dl     = ImGui::GetWindowDrawList();

    ImFont* h2 = S().fonts ? S().fonts->h2 : nullptr;
    if (h2) ImGui::PushFont(h2);
    const float lh = ImGui::GetTextLineHeight();

    // Optional accent glyph ahead of the label: a header reads as a section marker rather
    // than a bare underlined word, and the pane picks up the same iconography as its rows.
    const float iconSz = 15.0f * sc;
    if (icon && icon[0] && (ui::hasIconSdf(icon) || ui::getIcon(icon).valid)) {
        ui::drawIconGlass(dl, icon,
                          ImVec2(rowMin.x + iconSz * 0.5f, rowMin.y + lh * 0.5f), iconSz,
                          Palette::toU32(palette.accent), 0);
        ImGui::Dummy(ImVec2(iconSz, lh));
        ImGui::SameLine(0.0f, 6.0f * sc);
    }
    ImGui::TextUnformatted(label);
    if (h2) ImGui::PopFont();

    // Right-aligned secondary text (counts, state) on the header's own line — the pane no
    // longer has to spend a whole dim line under the header on a number.
    const float rowBot = ImGui::GetItemRectMax().y;
    if (trailing && trailing[0]) {
        const ImVec2 ts = ImGui::CalcTextSize(trailing);
        const float  tx = rowMin.x + rowW - ts.x;
        if (tx > ImGui::GetItemRectMax().x + 6.0f * sc)
            dl->AddText(ImVec2(tx, rowMin.y + (lh - ts.y) * 0.5f),
                        Palette::toU32(palette.textDim), trailing);
    }

    // Rule: accent fading to nothing across the pane, not a flat full-strength underline
    // clipped to the label — it reads as a section edge instead of a hyperlink.
    float a0[4], a1[4];
    Palette::withAlpha(palette.accent, a0, 0.55f);
    Palette::withAlpha(palette.accent, a1, 0.00f);
    const float ry = rowBot + 3.0f * sc;
    dl->AddRectFilledMultiColor(ImVec2(rowMin.x, ry),
                                ImVec2(rowMin.x + rowW, ry + 1.5f * sc),
                                Palette::toU32(a0), Palette::toU32(a1),
                                Palette::toU32(a1), Palette::toU32(a0));
    ImGui::Unindent(pad);
    ImGui::Dummy(ImVec2(0, 5.0f * sc));
}

ContentInset::ContentInset()  { ImGui::Indent(ui::metrics::partsPadX()); }
ContentInset::~ContentInset() { ImGui::Unindent(ui::metrics::partsPadX()); }

void emptyState(const char* iconName, const char* msg, const char* sub) {
    const float sc = ui::uiScale;
    float avail = ImGui::GetContentRegionAvail().y;
    ImGui::Dummy(ImVec2(0, avail * 0.35f));

    const float iconSz = 40.0f * sc;
    if (iconName && iconName[0]) {
        ImVec2 cur = ImGui::GetCursorScreenPos();
        float cx = cur.x + ImGui::GetContentRegionAvail().x * 0.5f;
        float dis[4];
        Palette::withAlpha(palette.textDisabled, dis, 0.6f);
        ui::drawIconGlass(ImGui::GetWindowDrawList(), iconName,
                          ImVec2(cx, cur.y + iconSz * 0.5f), iconSz,
                          Palette::toU32(dis), 0);
        ImGui::Dummy(ImVec2(0, iconSz + 4.0f * sc));
    }

    // Centered, not inset: an empty state owns the whole pane, so it deliberately ignores
    // the content line. One helper so the two lines can never drift apart.
    auto centeredLine = [](const char* text, const float role[4], float alpha) {
        ImGui::SetCursorPosX(ImGui::GetCursorPosX() +
                             (ImGui::GetContentRegionAvail().x -
                              ImGui::CalcTextSize(text).x) * 0.5f);
        ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(role[0], role[1], role[2], alpha));
        ImGui::TextUnformatted(text);
        ImGui::PopStyleColor();
    };
    centeredLine(msg, palette.textDim, 1.0f);
    if (sub) centeredLine(sub, palette.textDisabled, 0.8f);
    ImGui::Dummy(ImVec2(0, 8.0f * sc));
}

void spinner(const char* prefix) {
    // Every "working" indicator in the app is the brand mark in motion; busyLabel owns the
    // reserved square, the legibility floor and the accent tint the ASCII frames used to carry.
    ui::busyLabel(prefix);
}

// ---- domain glyph / part icons ---------------------------------------------------------

void domainGlyph(ImDrawList* dl, ImVec2 c, float r, const std::string& domain) {
    ImU32 col = ImGui::ColorConvertFloat4ToU32(
        ImVec4(palette.accent[0], palette.accent[1], palette.accent[2], 0.75f));
    ImU32 dim = ImGui::ColorConvertFloat4ToU32(
        ImVec4(palette.textDim[0], palette.textDim[1], palette.textDim[2], 0.55f));
    const float th = std::max(1.5f, r * 0.12f);
    if (domain == "fastener") {
        // Bolt: hex head + shank.
        ImVec2 head(c.x, c.y - r * 0.45f);
        dl->AddNgon(head, r * 0.42f, col, 6, th);
        dl->AddLine(ImVec2(c.x, head.y + r * 0.42f), ImVec2(c.x, c.y + r * 0.8f), col, th);
        for (int i = 0; i < 3; ++i) {
            float y = c.y + r * (0.25f + 0.22f * i);
            dl->AddLine(ImVec2(c.x - r * 0.22f, y), ImVec2(c.x + r * 0.22f, y), dim, th * 0.8f);
        }
    } else if (domain == "bearing") {
        dl->AddCircle(c, r * 0.85f, col, 0, th);
        dl->AddCircle(c, r * 0.38f, col, 0, th);
        for (int i = 0; i < 6; ++i) {
            float a = (float)i / 6.0f * 6.2831853f;
            dl->AddCircleFilled(ImVec2(c.x + cosf(a) * r * 0.61f, c.y + sinf(a) * r * 0.61f),
                                r * 0.10f, dim);
        }
    } else if (domain == "section") {
        // I-beam.
        float w = r * 0.8f, hh = r * 0.85f;
        dl->AddLine(ImVec2(c.x - w, c.y - hh), ImVec2(c.x + w, c.y - hh), col, th);
        dl->AddLine(ImVec2(c.x - w, c.y + hh), ImVec2(c.x + w, c.y + hh), col, th);
        dl->AddLine(ImVec2(c.x, c.y - hh), ImVec2(c.x, c.y + hh), col, th);
    } else if (domain == "fitting") {
        // Pipe elbow.
        dl->PathArcTo(ImVec2(c.x - r * 0.5f, c.y + r * 0.5f), r * 0.75f, -1.5708f, 0.0f);
        dl->PathStroke(col, 0, th * 1.6f);
        dl->AddLine(ImVec2(c.x - r * 0.5f, c.y - r * 0.25f), ImVec2(c.x - r * 0.9f, c.y - r * 0.25f), col, th * 1.6f);
        dl->AddLine(ImVec2(c.x + r * 0.25f, c.y + r * 0.5f), ImVec2(c.x + r * 0.25f, c.y + r * 0.9f), col, th * 1.6f);
    } else if (domain == "hydraulic") {
        // Cylinder + rod.
        dl->AddRect(ImVec2(c.x - r * 0.8f, c.y - r * 0.35f), ImVec2(c.x + r * 0.25f, c.y + r * 0.35f), col, 2.0f, 0, th);
        dl->AddLine(ImVec2(c.x + r * 0.25f, c.y), ImVec2(c.x + r * 0.85f, c.y), col, th);
        dl->AddLine(ImVec2(c.x - r * 0.35f, c.y - r * 0.35f), ImVec2(c.x - r * 0.35f, c.y + r * 0.35f), dim, th);
    } else if (domain == "ecad") {
        // IC chip.
        dl->AddRect(ImVec2(c.x - r * 0.5f, c.y - r * 0.5f), ImVec2(c.x + r * 0.5f, c.y + r * 0.5f), col, 2.0f, 0, th);
        for (int i = 0; i < 3; ++i) {
            float y = c.y - r * 0.3f + i * r * 0.3f;
            dl->AddLine(ImVec2(c.x - r * 0.8f, y), ImVec2(c.x - r * 0.5f, y), dim, th);
            dl->AddLine(ImVec2(c.x + r * 0.5f, y), ImVec2(c.x + r * 0.8f, y), dim, th);
        }
    } else if (domain == "catalog") {
        // Shopping tag.
        dl->AddTriangleFilled(ImVec2(c.x - r * 0.6f, c.y - r * 0.6f),
                              ImVec2(c.x + r * 0.1f, c.y - r * 0.6f),
                              ImVec2(c.x - r * 0.6f, c.y + r * 0.1f), dim);
        dl->AddRect(ImVec2(c.x - r * 0.6f, c.y - r * 0.6f), ImVec2(c.x + r * 0.6f, c.y + r * 0.6f), col, 3.0f, 0, th);
    } else {
        // Generic gear-ish circle.
        dl->AddCircle(c, r * 0.6f, col, 0, th);
        dl->AddCircle(c, r * 0.22f, dim, 0, th);
    }
}

const char* partIcon(const std::string& id) {
    // Taxonomy top-level ids (parts_hub taxonomy) + legacy per-item domains.
    struct Map { const char* key; const char* icon; };
    static const Map kMap[] = {
        // taxonomy top-level
        {"fasteners",             "cat_fasteners"},
        {"bearings_motion",       "cat_bearings"},
        {"structural_stock",      "cat_stock_metal"},
        {"drivetrain",            "cat_drivetrain"},
        {"wheels_tires_casters",  "cat_wheels"},
        {"tools",                 "cat_tools"},
        {"electronics",           "cat_electronics"},
        {"fluid_power",           "cat_hydraulics"},
        {"hardware_misc",         "cat_hardware"},
        {"adhesives_consumables", "material"},
        {"safety",                "warning_badge"},
        // legacy per-item domains
        {"fastener",  "cat_fasteners"},
        {"bearing",   "cat_bearings"},
        {"section",   "cat_stock_metal"},
        {"fitting",   "cat_hydraulics"},
        {"hydraulic", "cat_hydraulics"},
        {"ecad",      "cat_electronics"},
        {"catalog",   "parts_all"},
    };
    for (const Map& m : kMap)
        if (id == m.key) return m.icon;
    return nullptr;
}

void partIconDraw(ImDrawList* dl, ImVec2 center, float sizePx, const std::string& id,
                  bool selected) {
    const char* name = partIcon(id);
    if (name && (ui::hasIconSdf(name) || ui::getIcon(name).valid)) {
        // Tint pattern from ribbon_icon(): SDF glyphs take a palette role (accent when
        // selected/armed, text otherwise); multi-color raster glyphs keep atlas pixels.
        ImU32 col;
        if (ui::hasIconSdf(name)) {
            const float* role = selected ? palette.accent : palette.text;
            col = Palette::toU32(role);
        } else {
            col = IM_COL32(255, 255, 255, 255);
        }
        ui::drawIconGlass(dl, name, center, sizePx, col, 0);
    } else {
        domainGlyph(dl, center, sizePx * 0.5f, id);
    }
}

// ---- border-highlight rows -------------------------------------------------------------

bool borderRow(const char* strId, const char* label, const char* icon, bool selected,
               ImVec2 size, bool centerText, float rounding) {
    const float sc = ui::uiScale;
    ImDrawList* dl = ImGui::GetWindowDrawList();
    if (size.x <= 0.0f) size.x = ImGui::GetContentRegionAvail().x;
    if (size.y <= 0.0f) size.y = ImGui::GetFrameHeight();
    ImVec2 p0 = ImGui::GetCursorScreenPos();
    ImVec2 p1(p0.x + size.x, p0.y + size.y);

    bool clicked = ImGui::InvisibleButton(strId, size);
    bool hovered = ImGui::IsItemHovered();
    bool held    = ImGui::IsItemActive();

    if (hovered || held)
        dl->AddRectFilled(p0, p1, Palette::toU32(palette.treeHover), rounding * sc);

    // Selection = BORDER, not wash. Inset by 0.5px so the stroke never bleeds outside.
    ImVec2 b0(p0.x + 0.5f, p0.y + 0.5f), b1(p1.x - 0.5f, p1.y - 0.5f);
    if (selected)
        dl->AddRect(b0, b1, Palette::toU32(palette.accent), rounding * sc, 0, 1.5f * sc);
    else if (hovered)
        dl->AddRect(b0, b1, Palette::toU32(palette.accentMid), rounding * sc, 0, 1.0f * sc);

    // ---- content ----
    const float iconSz  = 18.0f * sc;
    const float iconGap = 6.0f * sc;
    const float padX    = ui::metrics::partsPadX();   // matches cards, cells and frames
    bool haveIcon = icon && icon[0] && (ui::hasIconSdf(icon) || ui::getIcon(icon).valid);

    float textW = (label && label[0]) ? ImGui::CalcTextSize(label).x : 0.0f;
    float textH = ImGui::GetTextLineHeight();
    float x;
    if (centerText) {
        float total = textW + (haveIcon ? iconSz + iconGap : 0.0f);
        x = p0.x + (size.x - total) * 0.5f;
    } else {
        x = p0.x + padX;
    }

    if (haveIcon) {
        ImU32 icol;
        if (ui::hasIconSdf(icon)) {
            const float* role = selected ? palette.accent : palette.text;
            icol = Palette::toU32(role);
        } else {
            icol = IM_COL32(255, 255, 255, 255);   // raster glyphs keep atlas pixels
        }
        ui::drawIconGlass(dl, icon, ImVec2(x + iconSz * 0.5f, p0.y + size.y * 0.5f),
                          iconSz, icol, 0);
        x += iconSz + iconGap;
    }

    if (label && label[0]) {
        // Vertically centered exactly; clip inside the row so long labels never overflow.
        // A CENTERED label is inset by (size.x - textW)/2, which on a snugly-sized row
        // (a chip) is smaller than padX — clipping at p1.x - padX then ate the last
        // character of every one of them. Centered rows clip on their own rect.
        dl->PushClipRect(p0, ImVec2(centerText ? p1.x : p1.x - padX, p1.y), true);
        dl->AddText(ImVec2(x, p0.y + (size.y - textH) * 0.5f),
                    Palette::toU32(palette.text), label);
        dl->PopClipRect();
    }
    return clicked;
}

// ---- chips -------------------------------------------------------------------------

float chipWidth(const char* label) {
    return ImGui::CalcTextSize(label).x + chipPadX() * 2.0f;
}

float chipPadX()  { return 10.0f * ui::uiScale; }
float chipHeight() { return ImGui::GetTextLineHeight() + 9.0f * ui::uiScale; }

bool chipToggle(const char* strId, const char* label, bool active) {
    const float sc   = ui::uiScale;
    const float padX = chipPadX();
    const ImVec2 ts  = ImGui::CalcTextSize(label);
    const ImVec2 sz(ts.x + padX * 2.0f, chipHeight());
    const ImVec2 p0 = ImGui::GetCursorScreenPos();
    const ImVec2 p1(p0.x + sz.x, p0.y + sz.y);

    const bool clicked = ImGui::InvisibleButton(strId, sz);
    const bool hovered = ImGui::IsItemHovered();
    const bool held    = ImGui::IsItemActive();

    ImDrawList* dl = ImGui::GetWindowDrawList();
    const float r  = sz.y * 0.5f;   // full pill
    if (active) {
        dl->AddRectFilled(p0, p1, Palette::toU32(palette.accentSoft), r);
        dl->AddRect(ImVec2(p0.x + 0.5f, p0.y + 0.5f), ImVec2(p1.x - 0.5f, p1.y - 0.5f),
                    Palette::toU32(palette.accent), r, 0, 1.2f * sc);
    } else {
        // A RESTING surface is the whole point: the old chips were invisible until hovered,
        // so a facet's options read as a ragged paragraph of clipped words.
        float fill[4];
        Palette::withAlpha(palette.frameBg, fill, held ? 1.0f : (hovered ? 0.95f : 0.70f));
        dl->AddRectFilled(p0, p1, Palette::toU32(fill), r);
        dl->AddRect(ImVec2(p0.x + 0.5f, p0.y + 0.5f), ImVec2(p1.x - 0.5f, p1.y - 0.5f),
                    Palette::toU32(hovered ? palette.accentMid : palette.border), r, 0,
                    1.0f * sc);
    }
    // Sized from the text, so no clip rect is needed — that is what used to truncate them.
    dl->AddText(ImVec2(p0.x + padX, p0.y + (sz.y - ts.y) * 0.5f),
                Palette::toU32(active ? palette.accent
                                      : (hovered ? palette.text : palette.textDim)),
                label);
    return clicked;
}

// ---- colour-coded liquid glass -------------------------------------------------------

namespace {

// One hue per taxonomy root. Picked as a spread around the wheel with the app's teal accent
// kept for Fasteners (the default landing category) so the pane still reads as Chudware and
// not as a paint chart. Saturation/value are deliberately uniform: only HUE encodes identity,
// so no category ever looks more important than another.
struct CatTint { const char* id; float rgb[3]; };
constexpr CatTint kCatTints[] = {
    {"fasteners",             {0.176f, 0.831f, 0.749f}},  // teal
    {"bearings_motion",       {0.380f, 0.690f, 0.980f}},  // sky
    {"structural_stock",      {0.620f, 0.680f, 0.780f}},  // steel
    {"drivetrain",            {0.960f, 0.640f, 0.310f}},  // amber
    {"wheels_tires_casters",  {0.560f, 0.780f, 0.400f}},  // moss
    {"tools",                 {0.930f, 0.520f, 0.420f}},  // clay
    {"electronics",           {0.560f, 0.520f, 0.960f}},  // indigo
    {"fluid_power",           {0.300f, 0.760f, 0.870f}},  // cyan
    {"hardware_misc",         {0.760f, 0.700f, 0.520f}},  // brass
    {"adhesives_consumables", {0.890f, 0.560f, 0.720f}},  // rose
    {"safety",                {0.960f, 0.760f, 0.300f}},  // hazard yellow
    {"electric_motors",       {0.450f, 0.620f, 0.950f}},  // cobalt
    {"motion_control",        {0.640f, 0.820f, 0.560f}},  // sage
    {"power_supplies",        {0.880f, 0.640f, 0.380f}},  // copper
    {"batteries",             {0.520f, 0.860f, 0.640f}},  // mint
    {"seals_gaskets",         {0.700f, 0.560f, 0.880f}},  // lilac
    {"electromechanical",     {0.480f, 0.740f, 0.840f}},  // slate cyan
    {"vendors",               {0.820f, 0.780f, 0.420f}},  // gold
    {"custom_fab",            {0.900f, 0.480f, 0.560f}},  // magenta
};

// Deterministic hue for an id with no entry above (vendor leaves, synthetic nodes). FNV-1a
// over the id keeps the colour stable across sessions — a category that changed colour on
// every launch would be worse than no colour at all.
void hashTint(const std::string& id, float out[3]) {
    unsigned h = 2166136261u;
    for (unsigned char c : id) { h ^= c; h *= 16777619u; }
    const float hue = (float)(h % 360u) / 360.0f;
    ImGui::ColorConvertHSVtoRGB(hue, 0.52f, 0.86f, out[0], out[1], out[2]);
}

}  // namespace

const float* categoryTint(const std::string& id) {
    // A child inherits its root's hue: the tree is a colour-coded MAP, and a branch whose
    // children each had their own colour would destroy the grouping the colour exists for.
    const std::string root = id.substr(0, id.find('.'));
    for (const auto& t : kCatTints)
        if (root == t.id) return t.rgb;
    // Cache hashed tints so the returned pointer stays valid for the caller's frame (and
    // beyond) — callers hand it straight to glassCardWash as a float[3].
    static std::map<std::string, std::array<float, 3>> cache;
    auto it = cache.find(root);
    if (it == cache.end()) {
        std::array<float, 3> rgb{};
        hashTint(root, rgb.data());
        it = cache.emplace(root, rgb).first;
    }
    return it->second.data();
}

bool glassRow(const char* strId, const char* label, const char* icon, bool selected,
              ImVec2 size, const float tint[3], float emphasis, float rounding) {
    const float sc = ui::uiScale;
    ImDrawList* dl = ImGui::GetWindowDrawList();
    if (size.x <= 0.0f) size.x = ImGui::GetContentRegionAvail().x;
    if (size.y <= 0.0f) size.y = ImGui::GetFrameHeight();
    const ImVec2 p0 = ImGui::GetCursorScreenPos();
    const ImVec2 p1(p0.x + size.x, p0.y + size.y);

    const bool clicked = ImGui::InvisibleButton(strId, size);
    const bool hovered = ImGui::IsItemHovered();
    const bool held    = ImGui::IsItemActive();

    // Resting wash, brighter on hover and brighter still when selected. The three levels are
    // one multiplier on ONE gradient so a row can never change its shape as it changes state.
    float wash = 0.13f * emphasis;
    if (hovered) wash = 0.22f * emphasis;
    if (held)    wash = 0.28f * emphasis;
    if (selected) wash = std::max(wash, 0.30f * emphasis);
    ui::glassCardWash(dl, p0, p1, rounding, tint, wash, 0.28f);

    // Left rail in the category hue: at a glance the pane reads as coloured bands even where
    // the wash itself is nearly invisible against a bright backdrop.
    float rail[4] = {tint[0], tint[1], tint[2], selected ? 0.95f : (hovered ? 0.80f : 0.55f)};
    const float railW = 3.0f * sc;
    dl->AddRectFilled(p0, ImVec2(p0.x + railW, p1.y), Palette::toU32(rail), rounding * sc,
                      ImDrawFlags_RoundCornersLeft);

    // Selection stays the app's border language, but in the row's OWN hue so the tree keeps
    // its colour coding while selected (an accent-teal border over a rose row read as a bug).
    if (selected) {
        float glow[4] = {tint[0], tint[1], tint[2], 1.0f};
        ui::glassSelectionGlow(dl, p0, p1, rounding, glow);   // dp — helper scales itself
    }

    // ---- content (same geometry as borderRow so the two never drift) ----
    const float iconSz  = 18.0f * sc;
    const float iconGap = 6.0f * sc;
    const float padX    = ui::metrics::partsPadX();
    const bool haveIcon = icon && icon[0] && (ui::hasIconSdf(icon) || ui::getIcon(icon).valid);
    float x = p0.x + padX;
    if (haveIcon) {
        // The glyph carries the hue, the label stays high-contrast text: tinting BOTH turned
        // a low-emphasis child row into unreadable coloured-on-dark text.
        ImU32 icol = IM_COL32(255, 255, 255, 255);
        if (ui::hasIconSdf(icon)) {
            float ic[4] = {tint[0], tint[1], tint[2], selected ? 1.0f : 0.92f};
            icol = Palette::toU32(ic);
        }
        ui::drawIconGlass(dl, icon, ImVec2(x + iconSz * 0.5f, p0.y + size.y * 0.5f),
                          iconSz, icol, 0);
        x += iconSz + iconGap;
    }
    if (label && label[0]) {
        const float textH = ImGui::GetTextLineHeight();
        dl->PushClipRect(p0, ImVec2(p1.x - padX, p1.y), true);
        dl->AddText(ImVec2(x, p0.y + (size.y - textH) * 0.5f),
                    Palette::toU32(palette.text), label);
        dl->PopClipRect();
    }
    return clicked;
}

void glassFieldBegin() {
    // Transparent stock frame — the gradient painted by glassFieldEnd IS the frame. Border
    // goes too: glassFieldEnd draws a hue-tinted one that tracks hover/active.
    ImGui::PushStyleColor(ImGuiCol_FrameBg,        ImVec4(0, 0, 0, 0));
    ImGui::PushStyleColor(ImGuiCol_FrameBgHovered, ImVec4(0, 0, 0, 0));
    ImGui::PushStyleColor(ImGuiCol_FrameBgActive,  ImVec4(0, 0, 0, 0));
    ImGui::GetWindowDrawList()->ChannelsSplit(2);
    ImGui::GetWindowDrawList()->ChannelsSetCurrent(1);
}

void glassFieldEnd(const float tint[3], float rounding) {
    const float sc = ui::uiScale;
    ImDrawList* dl = ImGui::GetWindowDrawList();
    const ImVec2 p0 = ImGui::GetItemRectMin();
    const ImVec2 p1 = ImGui::GetItemRectMax();
    const bool hovered = ImGui::IsItemHovered();
    const bool active  = ImGui::IsItemActive();

    dl->ChannelsSetCurrent(0);
    // Opaque base first: the field must not read as a hole punched in the pane, and the
    // wash alone is far too faint to seat text on.
    dl->AddRectFilled(p0, p1, Palette::toU32(active ? palette.frameBgActive
                                            : hovered ? palette.frameBgHovered
                                                      : palette.frameBg),
                      rounding * sc);
    ui::glassCardWash(dl, p0, p1, rounding, tint,
                      active ? 0.26f : (hovered ? 0.20f : 0.13f), 0.25f);
    float br[4] = {tint[0], tint[1], tint[2], active ? 0.90f : (hovered ? 0.62f : 0.34f)};
    dl->AddRect(ImVec2(p0.x + 0.5f, p0.y + 0.5f), ImVec2(p1.x - 0.5f, p1.y - 0.5f),
                Palette::toU32(br), rounding * sc, 0, (active ? 1.5f : 1.0f) * sc);
    dl->ChannelsMerge();
    ImGui::PopStyleColor(3);
}

void fieldLabel(const char* text) {
    ContentInset inset;
    ImGui::PushStyleColor(ImGuiCol_Text,
        ImVec4(palette.textDim[0], palette.textDim[1], palette.textDim[2], 0.95f));
    ImGui::TextUnformatted(text);
    ImGui::PopStyleColor();
}

void borderRowSurface(bool selected, float rows, float rounding) {
    const float sc = ui::uiScale;
    const float rowH = ImGui::GetFrameHeight() * rows;
    ImVec2 p0 = ImGui::GetCursorScreenPos();
    float  w  = ImGui::GetContentRegionAvail().x;
    ImVec2 p1(p0.x + w, p0.y + rowH);
    ImDrawList* dl = ImGui::GetWindowDrawList();
    // Resting glass surface (unchanged look), then border on hover/selection.
    ui::glassRowBackground(dl, p0, p1, rounding, false, false, 0.20f);
    bool hovered = ImGui::IsMouseHoveringRect(p0, p1);
    ImVec2 b0(p0.x + 0.5f, p0.y + 0.5f), b1(p1.x - 0.5f, p1.y - 0.5f);
    if (selected)
        dl->AddRect(b0, b1, Palette::toU32(palette.accent), rounding * sc, 0, 1.5f * sc);
    else if (hovered)
        dl->AddRect(b0, b1, Palette::toU32(palette.accentMid), rounding * sc, 0, 1.0f * sc);
}

void borderCardRect(ImDrawList* dl, ImVec2 p0, ImVec2 p1, bool selected, bool hovered,
                    float rounding) {
    const float sc = ui::uiScale;
    ImVec2 b0(p0.x + 0.5f, p0.y + 0.5f), b1(p1.x - 0.5f, p1.y - 0.5f);
    if (selected)
        dl->AddRect(b0, b1, Palette::toU32(palette.accent), rounding * sc, 0, 1.5f * sc);
    else if (hovered)
        dl->AddRect(b0, b1, Palette::toU32(palette.accentMid), rounding * sc, 0, 1.0f * sc);
}

// ---- thumbnails --------------------------------------------------------------------
//
// Part thumbnails and vendor logos ride ONE RPC (parts_thumbnail), ONE texture cache
// (S().thumbs) and ONE AsyncRpc dispatch thread, so they share a single request budget here.
//
// Cost model, measured against the live worker: an uncached standard part is RENDERED on
// demand (services/parts/gui_rpc.py::_render_local_part_png — parts_make → material/threads →
// doc_render_png) and takes 2-4 s; every later request for that id is a disk-cache hit at
// ~0 ms. AsyncRpc dispatches FIFO on a SINGLE thread, and drawResultsList/drawResultsGrid call
// requestThumb for every row every frame (no clipper), so a 100-row search used to hand the
// worker ~100 back-to-back renders: minutes of queue in which the queue/live-search/taxonomy
// polls behind them never ran and most cards never got their image. Bound the outstanding
// calls instead — the remaining rows are issued on later frames as slots free.
constexpr size_t kMaxThumbInFlight = 4;

// A transport failure (worker still booting, socket EOF, timeout) is NOT an answer, so it must
// never reach thumbFailed — a startup race would blank that card for the whole session. Retry
// it a bounded number of times with a widening gap, then stop.
constexpr int    kThumbTransportRetries = 3;
constexpr double kThumbRetryBackoffSec  = 2.0;

// Safety net for the budget above: AsyncRpc::shutdown() drops queued and undrained callbacks
// unrun, so an id can sit in thumbBusy with no callback ever arriving — and four such ids would
// wedge every thumbnail for the session. Past this deadline the id is RELEASED (never marked
// failed) so a later frame may ask again; the worker's disk cache makes the duplicate cheap.
// Comfortably above the 120 s RPC timeout plus the queue wait the in-flight cap permits.
constexpr double kThumbWatchdogSec = 600.0;

// Per-id request bookkeeping. Deliberately TU-local rather than PartsState: nothing outside
// this file reads it, and it is dropped the moment a request resolves either way.
struct ThumbReq {
    double startedAt = 0.0;   // ImGui::GetTime() when the call was handed to AsyncRpc
    double retryAt   = 0.0;   // earliest re-request after a transport failure
    int    attempts  = 0;     // transport failures so far
};
static std::map<std::string, ThumbReq> gThumbReq;

// A failed thumbnail is a permanent property of the record for this session — a part with no
// vendor image and no renderable geometry has nothing to show — so thumbFailed is a suppression
// list, not a cache. It must never be mass-cleared: dropping every id at once makes the next
// scroll re-fire a burst of RPCs that are all doomed to fail again. Bound it by refusing new
// entries once the cap is reached; the cap is set where a realistic session will never reach it.
// ONLY a definitive answer reaches here — transport failures retry, see issueThumbRequest.
static void markThumbFailed(const std::string& id) {
    constexpr size_t kFailedCap = 65536;
    if (S().thumbFailed.size() < kFailedCap) {
        S().thumbFailed.insert(id);
        gThumbReq.erase(id);   // suppressed for good — no retry bookkeeping left to keep
    } else {
        // Cap reached: the id cannot be recorded, so park it on a long backoff instead.
        // Suppression by other means; never a per-frame re-request storm.
        gThumbReq[id].retryAt = ImGui::GetTime() + kThumbWatchdogSec;
    }
}

void storeThumb(const std::string& id, const std::vector<unsigned char>& bytes) {
    int w = 0, h = 0, n = 0;
    unsigned char* px = stbi_load_from_memory(bytes.data(), (int)bytes.size(), &w, &h, &n, 4);
    if (!px) { markThumbFailed(id); return; }
    Thumb t;
    t.w = w; t.h = h;
    glGenTextures(1, &t.tex);
    glBindTexture(GL_TEXTURE_2D, t.tex);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
    glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA8, w, h, 0, GL_RGBA, GL_UNSIGNED_BYTE, px);
    stbi_image_free(px);
    const double now = ImGui::GetTime();
    t.lastUse = now;
    Thumb& slot = S().thumbs[id];
    if (slot.tex) glDeleteTextures(1, &slot.tex);  // never orphan a texture we overwrite
    slot = t;
    // LRU cap: a long browse session used to accumulate GL textures unboundedly (never
    // deleted). This runs inside rpc.drain() on the GL thread — the same thread that draws
    // the thumbs — so deleting the evicted texture right here is safe.
    //
    // 256 comfortably clears one screenful: the search asks for limit=100 (parts_workspace.cpp)
    // plus a handful of vendor logos. It is NOT raised further because a rendered part comes
    // back at 512x400 = 800 KiB of RGBA8, so the cap already fronts ~200 MiB of VRAM.
    // Entries touched THIS frame are pinned: drain() runs before NewFrame(), so every thumb the
    // previous frame drew carries exactly this timestamp. Without the pin, a result set larger
    // than the cap makes every lastUse tie and eviction picks an on-screen thumb, which the very
    // next frame re-requests and re-renders — a permanent churn that starves everything else.
    // Overshooting while that many thumbs are genuinely visible is the lesser evil; trimming
    // resumes the moment anything scrolls out of view.
    constexpr size_t kThumbCap = 256;
    while (S().thumbs.size() > kThumbCap) {
        auto oldest = S().thumbs.end();
        for (auto it = S().thumbs.begin(); it != S().thumbs.end(); ++it) {
            if (it->second.lastUse >= now) continue;   // drawn this frame — pinned
            if (oldest == S().thumbs.end() || it->second.lastUse < oldest->second.lastUse)
                oldest = it;
        }
        if (oldest == S().thumbs.end()) break;         // everything resident is on screen
        if (oldest->second.tex) glDeleteTextures(1, &oldest->second.tex);
        S().thumbs.erase(oldest);
    }
}

// Release ids whose callback can never arrive (see kThumbWatchdogSec). Walks at most
// kMaxThumbInFlight entries, and only when the budget is actually exhausted.
static void releaseStuckThumbs(double now) {
    for (auto it = S().thumbBusy.begin(); it != S().thumbBusy.end();) {
        auto q = gThumbReq.find(*it);
        if (q != gThumbReq.end() && now - q->second.startedAt > kThumbWatchdogSec)
            it = S().thumbBusy.erase(it);
        else
            ++it;
    }
}

// The one issue path for parts_thumbnail: single-flight, paced, and honest about which failures
// are permanent. `imageUrl` may be empty — the worker renders the part's own 3D model when it
// recognises the id, and answers ok:false when it genuinely has nothing to show.
static void issueThumbRequest(AppState& st, const std::string& id, const std::string& imageUrl) {
    if (S().thumbs.count(id) || S().thumbBusy.count(id) || S().thumbFailed.count(id))
        return;
    const double now = ImGui::GetTime();
    auto req = gThumbReq.find(id);
    if (req != gThumbReq.end() && req->second.retryAt > now)
        return;                                   // transport backoff still running
    if (S().thumbBusy.size() >= kMaxThumbInFlight) {
        releaseStuckThumbs(now);
        if (S().thumbBusy.size() >= kMaxThumbInFlight)
            return;                               // budget full — a later frame issues this one
    }
    gThumbReq[id].startedAt = now;
    S().thumbBusy.insert(id);
    nlohmann::json p;
    p["item_id"]   = id;
    p["image_url"] = imageUrl;
    st.rpc.call("parts_thumbnail", p, [id](const core::RpcResult& r) {
        // FIRST on every path: the in-flight slot is released whatever happens below.
        S().thumbBusy.erase(id);
        if (!r.ok) {
            // Transport-level failure — the worker never answered. Permanent only when it says
            // it has no such method (it will not grow one mid-session; same signal
            // parts_vendors.cpp reads for healthSupported). Everything else is retryable.
            if (r.err.find("unknown method") != std::string::npos) {
                markThumbFailed(id);
                return;
            }
            ThumbReq& q = gThumbReq[id];
            if (++q.attempts >= kThumbTransportRetries) { markThumbFailed(id); return; }
            q.retryAt = ImGui::GetTime() + kThumbRetryBackoffSec * q.attempts;
            return;
        }
        // The worker answered: ok:false means it looked and there is nothing to show.
        if (!r.result.is_object() || !r.result.value("ok", false)) { markThumbFailed(id); return; }
        const std::string b64 = r.result.value("image_b64", "");
        if (b64.empty()) { markThumbFailed(id); return; }
        storeThumb(id, b64decode(b64));   // marks failed itself if the bytes will not decode
        gThumbReq.erase(id);
    });
}

void requestThumb(AppState& st, const PartsItem& item) {
    if (S().thumbs.count(item.id) || S().thumbBusy.count(item.id) ||
        S().thumbFailed.count(item.id))
        return;
    // What the worker can actually answer for, verified against gui_rpc.py::_parts_thumbnail
    // and _resolve_core_family: (a) any record carrying a vendor image_url — fetched; (b) a
    // LOCAL record whose hub entry has geometry_ref.kind == "core_family" — RENDERED from its
    // own 3D model, no image_url required. `placeable` is precisely "geometry_ref is not None"
    // (gui_rpc.py:222) so it is the necessary condition for (b), and _resolve_core_family
    // refuses "catalog:" / live ids outright. Local ISO fastener rows arrive placeable + local
    // with an EMPTY image_url and do go to the worker — those are the rendered ones. Anything
    // else has no image on either side: record it once instead of paying an RPC per row for a
    // guaranteed ok:false. Either way the caller still paints partIconDraw's glyph, never a hole.
    const bool localRenderable = item.placeable && (item.source == "local" || item.source.empty());
    if (item.imageUrl.empty() && !localRenderable) { markThumbFailed(item.id); return; }
    issueThumbRequest(st, item.id, item.imageUrl);
}

void requestVendorLogo(AppState& st, const std::string& site, const std::string& domain) {
    if (site.empty() || domain.empty()) return;
    // Live vendor logo for the search-progress strip. Stored in the SAME texture cache as part
    // thumbs, keyed "vendorlogo:<site>", fetched through the existing parts_thumbnail RPC — so
    // it shares the in-flight budget with the part renders rather than jumping the queue.
    // A PNG-returning favicon resolver, not <domain>/favicon.ico: stb_image has no ICO decoder,
    // so the raw .ico would fetch and then fail to decode. Cached by item_id.
    issueThumbRequest(st, "vendorlogo:" + site,
                      "https://www.google.com/s2/favicons?domain=" + domain + "&sz=64");
}

}  // namespace parts
}  // namespace ui
