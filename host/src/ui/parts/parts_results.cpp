// Parts Library — results (list table + grid cards) and per-item actions.
// GUI V2 phase A: the list is a real ImGui table (Name | Vendor | Part# | Material | Price,
// natural clipping instead of the old printf %-36.36s column hack) with an 18px domain icon
// before the name; hover shows the border treatment + the Download/Insert actions.
#include <algorithm>
#include <cmath>
#include <cstdio>

#include "imgui.h"

#include "core/app_state.h"
#include "ui_theme.h"
#include "ui/metrics.h"    // ui::metrics::partsPadX — the ONE horizontal inset of this workspace

#include "parts_internal.h"
#include "parts_widgets.h"

namespace ui {
namespace parts {

// ---- per-item actions ------------------------------------------------------------------
// The action SET is collected before anything is drawn, so a grid card can MEASURE the
// wrapped button layout and size its hover band before painting it. A card must never put a
// button outside its own rounded rect — the old strip laid the buttons out on one unbounded
// line from the card's left pad and spilled them over the neighbouring card.
namespace {

enum class ActKind { Download, Insert, ReverseEngineer, GenerateModel };
struct ItemAction { const char* label; ActKind kind; };
constexpr int kMaxItemActions = 4;

int collectActions(const PartsItem& item, ItemAction* out) {
    int n = 0;
    const bool isCatalog = item.id.rfind("catalog:", 0) == 0;
    if (isCatalog && !item.downloaded) out[n++] = {"Download", ActKind::Download};
    out[n++] = {"Insert", ActKind::Insert};
    // Un-modeled parts (live vendor listings with no CAD to download) can be
    // reconstructed instead: hand their photos/datasheet/specs to a new Reverse
    // Engineering session. Offered on live results — the ones that often ship no model.
    if (item.source == "live") out[n++] = {"Reverse-engineer", ActKind::ReverseEngineer};
    // RE-32: "Reverse Engineer → Generate 3D model" — one-click internet-research
    // + parametric build for any part that has metadata but no placeable 3D model.
    // Hidden for parts that are already placeable (have a model attached).
    if (!item.placeable) out[n++] = {"Generate 3D model", ActKind::GenerateModel};
    IM_ASSERT(n <= kMaxItemActions);
    return n;
}

void runAction(AppState& st, const PartsItem& item, ActKind kind) {
    switch (kind) {
        case ActKind::Download:        queueDownload(st, item);            break;
        case ActKind::Insert:          insertItem(st, item);               break;
        case ActKind::ReverseEngineer: sendToReverseEngineering(st, item); break;
        case ActKind::GenerateModel:   generateModelFromLibrary(st, item); break;
    }
}

// Lays the buttons out from the current cursor and returns the total height.
//   maxW > 0  → the row is bounded: it wraps onto a new line (anchored back on the start x,
//               not on the window indent) and clamps a single over-wide button, so nothing
//               ever crosses maxW.
//   maxW <= 0 → one line, unbounded (list view: the table cell clips).
// `draw == false` runs the identical arithmetic without emitting anything — that is how the
// grid card reserves its band height before it paints the band.
float actionsLayout(AppState& st, const PartsItem& item, float maxW, bool draw) {
    ItemAction acts[kMaxItemActions];
    const int n = collectActions(item, acts);
    if (n == 0) return 0.0f;

    const float btnH = ImGui::GetTextLineHeight();     // compact: no vertical frame padding
    const float fpX  = ImGui::GetStyle().FramePadding.x;
    const float gap  = ui::metrics::U(1);
    const bool  wrap = maxW > 0.0f;
    const ImVec2 origin = ImGui::GetCursorScreenPos();
    if (draw) {
        ImGui::PushStyleVar(ImGuiStyleVar_FramePadding, ImVec2(fpX, 0.0f));
        ImGui::PushStyleVar(ImGuiStyleVar_ButtonTextAlign, ImVec2(0.0f, 0.5f));
    }

    float lineW = 0.0f, h = btnH;
    for (int i = 0; i < n; ++i) {
        float w = ImGui::CalcTextSize(acts[i].label).x + fpX * 2.0f;
        if (wrap && w > maxW) w = maxW;
        const bool nl = wrap && lineW > 0.0f && lineW + gap + w > maxW;
        if (nl) { lineW = 0.0f; h += gap + btnH; }
        if (draw) {
            if (nl)         ImGui::SetCursorScreenPos(ImVec2(origin.x, origin.y + h - btnH));
            else if (i > 0) ImGui::SameLine(0.0f, gap);
            if (ImGui::Button(acts[i].label, ImVec2(w, btnH)))
                runAction(st, item, acts[i].kind);
            if (acts[i].kind == ActKind::GenerateModel && ImGui::IsItemHovered())
                ImGui::SetTooltip("Reverse Engineer \xe2\x86\x92 Generate 3D model\n"
                                  "Pulls specs from the internet and builds a parametric\n"
                                  "model, then attaches it to this part so you can place it.");
        }
        lineW += (lineW > 0.0f ? gap : 0.0f) + w;
    }
    if (draw) ImGui::PopStyleVar(2);
    return h;
}

}  // namespace

static void itemTooltipAt(const PartsItem& item, bool hovered) {
    if (!hovered) return;
    ImGui::BeginTooltip();
    ImGui::TextUnformatted(item.name.c_str());
    if (!item.description.empty()) {
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(palette.textDim[0], palette.textDim[1], palette.textDim[2], 1.0f));
        ImGui::PushTextWrapPos(360.0f * ui::uiScale);
        ImGui::TextUnformatted(item.description.c_str());
        ImGui::PopTextWrapPos();
        ImGui::PopStyleColor();
    }
    if (!item.material.empty()) ImGui::Text("Material: %s", item.material.c_str());
    if (!item.standard.empty()) ImGui::Text("Standard: %s", item.standard.c_str());
    if (!item.license.empty())  ImGui::TextDisabled("License: %s", item.license.c_str());
    if (!item.datasheet.empty()) ImGui::TextDisabled("Datasheet: %s", item.datasheet.c_str());
    ImGui::TextDisabled("%s", item.source == "live" ? "Live vendor result" : "Local library");
    ImGui::EndTooltip();
}

static void itemTooltip(const PartsItem& item) {
    itemTooltipAt(item, ImGui::IsItemHovered(ImGuiHoveredFlags_DelayNormal));
}

// Searching / error / empty preamble, shared by both renderers so a backend failure can
// never be misreported as "No results" in one view but not the other. Returns true when it
// painted a state and the caller must return without drawing results; each renderer passes
// its own empty-state copy.
static bool resultsPreamble(const char* noResultsSub, const char* promptSub) {
    if (S().searching) { spinner("Searching"); return true; }
    if (!S().searchErr.empty()) {
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(palette.statusErr[0], palette.statusErr[1],
                   palette.statusErr[2], 1.0f));
        ImGui::TextWrapped("Error: %s", S().searchErr.c_str());
        ImGui::PopStyleColor();
        return true;
    }
    if (S().results.empty()) {
        if (S().liveSearching) { spinner("Searching vendors"); return true; }
        if (S().searchDone)
            emptyState("cancel_x", "No results", noResultsSub);
        else
            emptyState("parts_all", "Search for parts", promptSub);
        return true;
    }
    return false;
}

void drawResultsList(AppState& st) {
    const float sc = ui::uiScale;

    if (resultsPreamble("Try a different search term, category, or loosen the filters.",
                        "Type in the search bar or pick a category to browse."))
        return;

    // The table's OUTER padding is the list's content inset: PadOuterX + a partsPadX cell
    // padding put the column headers, the row icon and every cell on the pane's text line —
    // the same line the section header above the table and a grid card's title sit on. The
    // row highlight still spans the full pane width, exactly like a card's rounded rect.
    // Pushed before the column budget below, which measures the chrome from it.
    ImGui::PushStyleVar(ImGuiStyleVar_CellPadding,
                        ImVec2(ui::metrics::partsPadX(), ImGui::GetStyle().CellPadding.y));

    // Narrow-pane column budget: the fixed columns keep their full width no matter how
    // little is left, which crushes the stretch "Name" column — the one telling you what the
    // part actually is — to nothing when the pane is squeezed (AI Copilot docked alongside).
    // Below the threshold the lowest-value columns yield, Material first then Vendor, so
    // Name always keeps a readable minimum. No-op at any normal pane width.
    const float wVendor = 110.0f * sc, wPart = 90.0f * sc, wMaterial = 90.0f * sc;
    const float wPrice  =  80.0f * sc, wAct  = 140.0f * sc;
    const float nameMin = 150.0f * sc;
    // PadOuterX chrome is exactly 2*CellPadding.x per column (outer padding both ends +
    // inner spacing between each pair), i.e. 2 per cell x 6 cells.
    const float chrome  = 6.0f * ImGui::GetStyle().CellPadding.x * 2.0f;
    const float budget  = ImGui::GetContentRegionAvail().x - chrome - nameMin;
    const bool showMaterial = budget >= wVendor + wPart + wMaterial + wPrice + wAct;
    const bool showVendor   = budget >= wVendor + wPart + wPrice + wAct;
    const ImGuiTableColumnFlags colVendor = ImGuiTableColumnFlags_WidthFixed |
        (showVendor   ? 0 : ImGuiTableColumnFlags_Disabled);
    const ImGuiTableColumnFlags colMaterial = ImGuiTableColumnFlags_WidthFixed |
        (showMaterial ? 0 : ImGuiTableColumnFlags_Disabled);

    const ImGuiTableFlags tblFlags = ImGuiTableFlags_SizingFixedFit |
                                     ImGuiTableFlags_NoBordersInBody |
                                     ImGuiTableFlags_PadOuterX;
    if (!ImGui::BeginTable("##parts_tbl", 6, tblFlags)) { ImGui::PopStyleVar(); return; }
    ImGui::TableSetupColumn("Name",     ImGuiTableColumnFlags_WidthStretch);
    ImGui::TableSetupColumn("Vendor",   colVendor,   wVendor);
    ImGui::TableSetupColumn("Part #",   ImGuiTableColumnFlags_WidthFixed, wPart);
    ImGui::TableSetupColumn("Material", colMaterial, wMaterial);
    ImGui::TableSetupColumn("Price",    ImGuiTableColumnFlags_WidthFixed, wPrice);
    ImGui::TableSetupColumn("##act",    ImGuiTableColumnFlags_WidthFixed, wAct);

    // Header row (textDim, no ugly TableHeadersRow chrome).
    ImGui::TableNextRow();
    ImGui::PushStyleColor(ImGuiCol_Text,
        ImVec4(palette.textDim[0], palette.textDim[1], palette.textDim[2], 1.0f));
    const char* heads[5] = {"Name", "Vendor", "Part #", "Material", "Price"};
    for (int c = 0; c < 5; ++c) {
        ImGui::TableSetColumnIndex(c);
        ImGui::TextUnformatted(heads[c]);
    }
    ImGui::PopStyleColor();

    const float rowH   = ImGui::GetFrameHeight();
    const float iconSz = 18.0f * sc;
    ImDrawList* dl = ImGui::GetWindowDrawList();

    for (auto& item : S().results) {
        ImGui::PushID(item.id.c_str());
        ImGui::TableNextRow(ImGuiTableRowFlags_None, rowH);
        ImGui::TableSetColumnIndex(0);

        // Row-spanning selectable: gives us hover state; actions overlap it.
        ImVec2 rowPos = ImGui::GetCursorScreenPos();
        ImGui::PushStyleColor(ImGuiCol_HeaderHovered, ImVec4(0, 0, 0, 0));
        ImGui::PushStyleColor(ImGuiCol_HeaderActive,  ImVec4(0, 0, 0, 0));
        ImGui::Selectable("##row", false,
                          ImGuiSelectableFlags_SpanAllColumns |
                          ImGuiSelectableFlags_AllowOverlap,
                          ImVec2(0, rowH));
        ImGui::PopStyleColor(2);
        bool hovered = ImGui::IsItemHovered(
            ImGuiHoveredFlags_AllowWhenOverlappedByItem | ImGuiHoveredFlags_AllowWhenBlockedByActiveItem);
        ImVec2 rMin = ImGui::GetItemRectMin();
        ImVec2 rMax = ImGui::GetItemRectMax();
        if (hovered) {
            // Border treatment (hover): treeHover wash + accentMid border, inset 0.5px.
            dl->AddRectFilled(rMin, rMax, Palette::toU32(palette.treeHover), 4.0f * sc);
            dl->AddRect(ImVec2(rMin.x + 0.5f, rMin.y + 0.5f),
                        ImVec2(rMax.x - 0.5f, rMax.y - 0.5f),
                        Palette::toU32(palette.accentMid), 4.0f * sc, 0, 1.0f * sc);
        }
        itemTooltipAt(item, hovered && ImGui::IsItemHovered(ImGuiHoveredFlags_DelayNormal |
                                                            ImGuiHoveredFlags_AllowWhenOverlappedByItem));

        // ---- Name cell: thumbnail (or domain icon fallback) + status badges + name ----
        ImGui::SetCursorScreenPos(rowPos);
        requestThumb(st, item);
        auto th = S().thumbs.find(item.id);
        if (th != S().thumbs.end() && th->second.tex) {
            th->second.lastUse = ImGui::GetTime();  // LRU: keep on-screen thumbs resident
            // Fit the product image into the icon box, centered, preserving aspect.
            const float iw = (float)th->second.w, ih = (float)th->second.h;
            const float fit = std::min(iconSz / iw, iconSz / ih);
            const float dw = iw * fit, dh = ih * fit;
            const ImVec2 c(rowPos.x + iconSz * 0.5f, rowPos.y + rowH * 0.5f);
            dl->AddImage((ImTextureID)(intptr_t)th->second.tex,
                         ImVec2(c.x - dw * 0.5f, c.y - dh * 0.5f),
                         ImVec2(c.x + dw * 0.5f, c.y + dh * 0.5f));
        } else {
            partIconDraw(dl, ImVec2(rowPos.x + iconSz * 0.5f, rowPos.y + rowH * 0.5f),
                         iconSz, item.domain);
        }
        ImGui::SetCursorScreenPos(ImVec2(rowPos.x + iconSz + 6.0f * sc,
                                         rowPos.y + (rowH - ImGui::GetTextLineHeight()) * 0.5f));
        if (item.downloaded) {
            ImGui::PushStyleColor(ImGuiCol_Text,
                ImVec4(palette.statusOk[0], palette.statusOk[1],
                       palette.statusOk[2], 1.0f));
            ImGui::TextUnformatted("[v]");
            ImGui::PopStyleColor();
            ImGui::SameLine();
        }
        if (item.source == "live") {
            ImGui::PushStyleColor(ImGuiCol_Text,
                ImVec4(palette.accent[0], palette.accent[1], palette.accent[2], 0.9f));
            ImGui::TextUnformatted("[live]");
            ImGui::PopStyleColor();
            ImGui::SameLine();
        }
        ImGui::TextUnformatted(item.name.c_str());   // table clipping ellipsizes naturally

        auto dimCell = [&](int col, const char* text) {
            ImGui::TableSetColumnIndex(col);
            ImGui::PushStyleColor(ImGuiCol_Text,
                ImVec4(palette.textDim[0], palette.textDim[1], palette.textDim[2], 1.0f));
            ImGui::TextUnformatted(text);
            ImGui::PopStyleColor();
        };
        dimCell(1, item.vendor.c_str());
        dimCell(2, item.partNumber.c_str());
        dimCell(3, item.material.empty() ? "-" : item.material.c_str());

        ImGui::TableSetColumnIndex(4);
        if (item.priceUsd > 0.0)
            ImGui::Text("$%.2f/%s", item.priceUsd, item.unit.c_str());
        else
            ImGui::TextDisabled("—");

        ImGui::TableSetColumnIndex(5);
        if (hovered) {
            ImGui::PushStyleColor(ImGuiCol_Button,
                ImVec4(palette.button[0], palette.button[1],
                       palette.button[2], palette.button[3]));
            actionsLayout(st, item, /*maxW=*/0.0f, /*draw=*/true);
            ImGui::PopStyleColor();
        }

        ImGui::PopID();
    }
    ImGui::EndTable();
    ImGui::PopStyleVar();  // CellPadding
}

// Grid cards: deterministic column layout. Each card reserves a FIXED cardW×cardH
// footprint first (InvisibleButton, AllowOverlap so the hover action buttons drawn on
// top still click), then paints everything with the drawlist relative to that rect,
// clipped to the card — free-floating text/SameLine bookkeeping was cascading cards
// diagonally and letting long names bleed past the card edge (WS4a regression).
// Every horizontal offset inside a card is partsPadX(): the thumbnail, the title, the part
// number, the price and the hover action row all start on ONE content line, and the row of
// buttons wraps inside the content width instead of spilling over the next card.
void drawResultsGrid(AppState& st) {
    const float sc    = ui::uiScale;
    const float pad   = ui::metrics::partsPadX();
    const float cardH = 168.0f * sc;
    const float imgH  = 88.0f * sc;
    const float gap   = 8.0f * sc;
    const float cardRound = 8.0f;      // logical corner; the helpers scale it themselves

    if (resultsPreamble("Try a different search term or category.",
                        "Type in the search bar or pick a category."))
        return;

    // Column packing: n cards fit when n*(cardW+gap) <= availW+gap. The grid stays LEFT
    // aligned on the pane's content origin — the same line the "Parts (N)" section header
    // starts at — instead of the old centering, which pushed the first card off that line by
    // up to half a column. A card is also never wider than the pane it sits in.
    const float availW   = ImGui::GetContentRegionAvail().x;
    const float cardW    = std::min(168.0f * sc, availW);
    const int   cols     = std::max(1, (int)((availW + gap) / (cardW + gap)));
    const float startX   = ImGui::GetCursorPosX();
    // The card reserves `pad` on BOTH sides; the floor keeps a squeezed pane from producing a
    // negative content width (which would make the ellipsize loop and AddImage draw garbage).
    const float contentW = std::max(ui::metrics::U(4), cardW - 2.0f * pad);
    const float lineH    = ImGui::GetTextLineHeight();
    ImDrawList* dl       = ImGui::GetWindowDrawList();

    ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing, ImVec2(gap, gap));  // uniform row gap
    int i = 0;
    for (auto& item : S().results) {
        const int col = i % cols;
        if (col != 0)
            ImGui::SameLine(startX + col * (cardW + gap));   // absolute → columns stay aligned
        ImGui::PushID(item.id.c_str());

        // ---- fixed footprint + hover/tooltip item -----------------------------------
        const ImVec2 p0 = ImGui::GetCursorScreenPos();
        const ImVec2 p1(p0.x + cardW, p0.y + cardH);
        const float  cx = p0.x + pad;        // the card's ONE content line
        ImGui::SetNextItemAllowOverlap();
        ImGui::InvisibleButton("##card", ImVec2(cardW, cardH));
        itemTooltipAt(item, ImGui::IsItemHovered(ImGuiHoveredFlags_DelayNormal));
        const bool hov = ImGui::IsMouseHoveringRect(p0, p1);  // stays true over the buttons

        // Resting glass surface stays; hover reads by BORDER, not a stronger wash.
        ui::glassRowBackground(dl, p0, p1, cardRound, false, false, 0.20f);
        borderCardRect(dl, p0, p1, /*selected=*/false, hov, cardRound);

        dl->PushClipRect(p0, p1, true);

        // ---- preview band: thumbnail when available, domain icon/glyph otherwise ----
        requestThumb(st, item);
        auto th = S().thumbs.find(item.id);
        if (th != S().thumbs.end() && th->second.tex) {
            th->second.lastUse = ImGui::GetTime();  // LRU: keep on-screen thumbs resident
            const float iw = (float)th->second.w, ih = (float)th->second.h;
            const float fit = std::min(contentW / iw, (imgH - pad) / ih);
            const float dw = iw * fit, dh = ih * fit;
            const ImVec2 i0(cx + (contentW - dw) * 0.5f, p0.y + pad * 0.5f + (imgH - dh) * 0.5f);
            dl->AddImage((ImTextureID)(intptr_t)th->second.tex, i0,
                         ImVec2(i0.x + dw, i0.y + dh));
        } else {
            partIconDraw(dl, ImVec2(cx + contentW * 0.5f, p0.y + imgH * 0.5f + 4.0f * sc),
                         std::min(52.0f * sc, contentW), item.domain);
        }

        // ---- text lines: all three start on the content line, ellipsized to contentW ----
        auto contentText = [&](float y, const char* t, ImU32 colU32) {
            std::string s(t);
            if (ImGui::CalcTextSize(s.c_str()).x > contentW) {
                while (!s.empty() &&
                       ImGui::CalcTextSize((s + "…").c_str()).x > contentW) {
                    // pop one UTF-8 code point, not one byte
                    do { s.pop_back(); } while (!s.empty() &&
                                                ((unsigned char)s.back() & 0xC0) == 0x80);
                }
                s += "…";
            }
            dl->AddText(ImVec2(cx, y), colU32, s.c_str());
        };
        float ty = p0.y + imgH + 6.0f * sc;
        contentText(ty, item.name.c_str(), ImGui::GetColorU32(ImGuiCol_Text));
        ty += lineH + 2.0f * sc;
        if (!item.partNumber.empty()) {
            contentText(ty, item.partNumber.c_str(), Palette::toU32(palette.textDim));
            ty += lineH + 2.0f * sc;
        }
        char pbuf[48];
        if (item.priceUsd > 0.0)
            std::snprintf(pbuf, sizeof(pbuf), "$%.2f%s", item.priceUsd,
                          item.source == "live" ? "  [live]" : "");
        else
            std::snprintf(pbuf, sizeof(pbuf), "%s",
                          item.source == "live" ? "[live]" : "—");
        contentText(ty, pbuf, ImGui::ColorConvertFloat4ToU32(
                                  ImVec4(palette.accent[0], palette.accent[1],
                                         palette.accent[2], 1.0f)));

        dl->PopClipRect();

        // ---- hover actions: a bottom band INSIDE the card rect ------------------------
        // Measured first so the band grows upward to fit however many lines the buttons wrap
        // onto, and hard-clipped to the band: an item with four actions can never spill onto
        // its neighbour. The buttons start on the same content line as the title above them.
        if (hov) {
            const float bandPadY = 4.0f * sc;
            const float actH     = actionsLayout(st, item, contentW, /*draw=*/false);
            if (actH > 0.0f) {
                const float bandTop = std::max(p0.y, p1.y - actH - 2.0f * bandPadY);
                const ImVec2 b0(p0.x, bandTop), b1(p1.x, p1.y);
                // Opaque base BEFORE the glass tint: glassBg is translucent (alpha .87),
                // so with four wrapped action rows the card's own title showed through the
                // band and read as overlapping text.
                dl->AddRectFilled(b0, b1, Palette::toU32(palette.panelBg), cardRound * sc,
                                  ImDrawFlags_RoundCornersBottom);
                dl->AddRectFilled(b0, b1, Palette::toU32(palette.glassBg), cardRound * sc,
                                  ImDrawFlags_RoundCornersBottom);
                ImGui::PushClipRect(b0, b1, true);
                ImGui::PushStyleColor(ImGuiCol_Button,
                    ImVec4(palette.button[0], palette.button[1],
                           palette.button[2], palette.button[3]));
                ImGui::SetCursorScreenPos(ImVec2(cx, bandTop + bandPadY));
                actionsLayout(st, item, contentW, /*draw=*/true);
                ImGui::PopStyleColor();
                ImGui::PopClipRect();
            }
            // Re-anchor layout on the card rect so the buttons' line doesn't skew the
            // next SameLine/new-row bookkeeping.
            ImGui::SetCursorScreenPos(p0);
            ImGui::Dummy(ImVec2(cardW, cardH));
        }

        ImGui::PopID();
        ++i;
    }
    ImGui::PopStyleVar();  // ItemSpacing
}

}  // namespace parts
}  // namespace ui
