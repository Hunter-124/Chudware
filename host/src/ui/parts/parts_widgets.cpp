// Parts Library shared widgets — see parts_widgets.h. GUI V2 phase A: the border-highlight
// selection language (selection = border, not wash) + atlas icons via drawIconGlass.
#include "parts_widgets.h"

#include <algorithm>
#include <cmath>

#include <glad/gl.h>

#include "stb_image.h"

#include "core/app_state.h"
#include "ui/icons.h"
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

void sectionHeader(const char* label, bool firstInPane) {
    const float sc = ui::uiScale;
    if (!firstInPane) ImGui::Dummy(ImVec2(0, 6.0f * sc));
    ImFont* h2 = S().fonts ? S().fonts->h2 : nullptr;
    if (h2) ImGui::PushFont(h2);
    ImGui::TextUnformatted(label);
    if (h2) ImGui::PopFont();
    ImVec2 p0 = ImGui::GetItemRectMin();
    ImVec2 p1 = ImGui::GetItemRectMax();
    ImGui::GetWindowDrawList()->AddLine(
        ImVec2(p0.x, p1.y + 1.0f * sc), ImVec2(p1.x, p1.y + 1.0f * sc),
        ImGui::ColorConvertFloat4ToU32(
            ImVec4(palette.accent[0], palette.accent[1], palette.accent[2], 0.60f)),
        1.0f * sc);
    ImGui::Spacing();
}

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

    {
        float mw = ImGui::CalcTextSize(msg).x;
        ImGui::SetCursorPosX(ImGui::GetCursorPosX() +
                             (ImGui::GetContentRegionAvail().x - mw) * 0.5f);
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(palette.textDim[0], palette.textDim[1], palette.textDim[2], 1.0f));
        ImGui::TextUnformatted(msg);
        ImGui::PopStyleColor();
    }
    if (sub) {
        float sw = ImGui::CalcTextSize(sub).x;
        ImGui::SetCursorPosX(ImGui::GetCursorPosX() +
                             (ImGui::GetContentRegionAvail().x - sw) * 0.5f);
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(palette.textDisabled[0], palette.textDisabled[1],
                   palette.textDisabled[2], 0.8f));
        ImGui::TextUnformatted(sub);
        ImGui::PopStyleColor();
    }
    ImGui::Dummy(ImVec2(0, 8.0f * sc));
}

void spinner(const char* prefix) {
    static const char* kFrames[] = {". . .", "o . .", ". o .", ". . o"};
    int f = (int)(ImGui::GetTime() * 4.0) & 3;
    ImGui::PushStyleColor(ImGuiCol_Text,
        ImVec4(palette.accent[0], palette.accent[1], palette.accent[2], 0.85f));
    ImGui::Text("%s %s", prefix, kFrames[f]);
    ImGui::PopStyleColor();
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
    const float padX    = 8.0f * sc;
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
        dl->PushClipRect(p0, ImVec2(p1.x - 4.0f * sc, p1.y), true);
        dl->AddText(ImVec2(x, p0.y + (size.y - textH) * 0.5f),
                    Palette::toU32(palette.text), label);
        dl->PopClipRect();
    }
    return clicked;
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

void storeThumb(const std::string& id, const std::vector<unsigned char>& bytes) {
    int w = 0, h = 0, n = 0;
    unsigned char* px = stbi_load_from_memory(bytes.data(), (int)bytes.size(), &w, &h, &n, 4);
    if (!px) { S().thumbFailed.insert(id); return; }
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
    S().thumbs[id] = t;
}

void requestThumb(AppState& st, const PartsItem& item) {
    if (S().thumbs.count(item.id) || S().thumbBusy.count(item.id) ||
        S().thumbFailed.count(item.id))
        return;
    // Standard/library parts carry no vendor image_url — the backend renders their actual 3D
    // CAD model on demand (cached by item_id). Still fire the RPC for a placeable local part;
    // only give up when there's neither a URL nor a renderable local part.
    const bool localRenderable = item.placeable && (item.source == "local" || item.source.empty());
    if (item.imageUrl.empty() && !localRenderable) { S().thumbFailed.insert(item.id); return; }
    S().thumbBusy.insert(item.id);
    nlohmann::json p;
    p["item_id"]   = item.id;
    p["image_url"] = item.imageUrl;
    st.rpc.call("parts_thumbnail", p, [id = item.id](const core::RpcResult& r) {
        S().thumbBusy.erase(id);
        if (!r.ok || !r.result.is_object() || !r.result.value("ok", false)) {
            S().thumbFailed.insert(id);
            return;
        }
        std::string b64 = r.result.value("image_b64", "");
        if (b64.empty()) { S().thumbFailed.insert(id); return; }
        storeThumb(id, b64decode(b64));
    });
}

void requestVendorLogo(AppState& st, const std::string& site, const std::string& domain) {
    // Live vendor logo for the search-progress strip. Stored in the SAME texture cache as
    // part thumbs, keyed "vendorlogo:<site>", fetched through the existing parts_thumbnail RPC.
    const std::string id = "vendorlogo:" + site;
    if (site.empty() || domain.empty() ||
        S().thumbs.count(id) || S().thumbBusy.count(id) || S().thumbFailed.count(id))
        return;
    S().thumbBusy.insert(id);
    nlohmann::json p;
    p["item_id"] = id;
    // The vendor's live favicon via a PNG-returning resolver — stb_image has no ICO decoder,
    // so a raw <domain>/favicon.ico would fetch but fail to decode. Cached by item_id.
    p["image_url"] = "https://www.google.com/s2/favicons?domain=" + domain + "&sz=64";
    st.rpc.call("parts_thumbnail", p, [id](const core::RpcResult& r) {
        S().thumbBusy.erase(id);
        if (!r.ok || !r.result.is_object() || !r.result.value("ok", false)) {
            S().thumbFailed.insert(id);
            return;
        }
        std::string b64 = r.result.value("image_b64", "");
        if (b64.empty()) { S().thumbFailed.insert(id); return; }
        storeThumb(id, b64decode(b64));
    });
}

}  // namespace parts
}  // namespace ui
