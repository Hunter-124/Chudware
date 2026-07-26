// Parts Library — results (list table + grid cards) and per-item actions.
// GUI V2 phase A: the list is a real ImGui table (Name | Vendor | Part# | Material | Price,
// natural clipping instead of the old printf %-36.36s column hack) with an 18px domain icon
// before the name; hover shows the border treatment + the item's actions.
// Status ("downloaded", "live vendor result") is carried by drawn badges/glyphs — the ASCII
// bracket tags they replace read as debug output and shoved the real text sideways.
#include <algorithm>
#include <cfloat>
#include <cmath>
#include <cstdio>
#include <string>

#include "imgui.h"

#include "core/app_state.h"
#include "ui_theme.h"
#include "ui/icons.h"      // ui::drawIconGlass — atlas glyphs on the card preview band
#include "ui/metrics.h"    // ui::metrics::partsPadX — the ONE horizontal inset of this workspace

#include "parts_internal.h"
#include "parts_widgets.h"

namespace ui {
namespace parts {

// ---- per-item actions ------------------------------------------------------------------
// A card must never put a button outside its own rounded rect — the old strip laid the
// buttons out on one unbounded line from the card's left pad and spilled them over the
// neighbouring card. The grid now shows only the PRIMARY action plus a "…" overflow popup,
// so its hover band is a single text line instead of a measured, wrapped four-button block
// that grew until it covered the thumbnail and the title it was meant to annotate.
namespace {

enum class ActKind { Download, Insert, ReverseEngineer, GenerateModel };
struct ItemAction { const char* label; ActKind kind; };
constexpr int kMaxItemActions = 4;

// Shown on the "Generate 3D model" entry wherever it appears (list row, grid overflow popup).
constexpr const char* kGenModelTip =
    "Reverse Engineer \xe2\x86\x92 Generate 3D model\n"
    "Pulls specs from the internet and builds a parametric\n"
    "model, then attaches it to this part so you can place it.";

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

// ---- status badges -----------------------------------------------------------------------
// A rounded pill (opaque base + role wash + role hairline + role text) replacing the ASCII
// bracket tags this file used to print: they read as debug output, and on a grid card the
// live tag was concatenated onto the price string, so the price shared its line with a
// status marker. The label renders a notch under body size so the pill stays ~14dp tall
// beside a full text line.
float badgeTextSize() { return ImGui::GetFontSize() * 0.82f; }

float badgeH() {
    return std::max(14.0f * ui::uiScale, badgeTextSize() + 4.0f * ui::uiScale);
}

float badgeW(const char* label) {
    ImFont* f = ImGui::GetFont();
    return f->CalcTextSizeA(badgeTextSize(), FLT_MAX, 0.0f, label).x + 10.0f * ui::uiScale;
}

void drawBadge(ImDrawList* dl, ImVec2 p0, const char* label, const float role[4]) {
    const float sc = ui::uiScale;
    const float fs = badgeTextSize();
    ImFont* f = ImGui::GetFont();
    const ImVec2 ts = f->CalcTextSizeA(fs, FLT_MAX, 0.0f, label);
    const ImVec2 p1(p0.x + ts.x + 10.0f * sc, p0.y + badgeH());
    const float r = (p1.y - p0.y) * 0.5f;    // full pill
    float wash[4], line[4];
    Palette::withAlpha(role, wash, 0.22f);
    Palette::withAlpha(role, line, 0.55f);
    // Opaque base BEFORE the wash: a grid badge sits on top of a product photo, where a
    // purely translucent chip takes the photo's colour and stops reading as a badge.
    dl->AddRectFilled(p0, p1, Palette::toU32(palette.panelBg), r);
    dl->AddRectFilled(p0, p1, Palette::toU32(wash), r);
    dl->AddRect(ImVec2(p0.x + 0.5f, p0.y + 0.5f), ImVec2(p1.x - 0.5f, p1.y - 0.5f),
                Palette::toU32(line), r, 0, ui::metrics::stroke(1.0f));
    dl->AddText(f, fs, ImVec2(p0.x + (p1.x - p0.x - ts.x) * 0.5f,
                              p0.y + (p1.y - p0.y - ts.y) * 0.5f),
                Palette::toU32(role), label);
}

// Layout-participating badge for the list rows: reserves its own footprint with a Dummy so
// the SameLine flow (badges, then the name) is unchanged, centred on the current text line.
void badgeInline(const char* label, const float role[4]) {
    const ImVec2 cur = ImGui::GetCursorScreenPos();
    const float  lh  = ImGui::GetTextLineHeight();
    drawBadge(ImGui::GetWindowDrawList(),
              ImVec2(cur.x, cur.y + (lh - badgeH()) * 0.5f), label, role);
    ImGui::Dummy(ImVec2(badgeW(label), lh));
}

// ---- source pills --------------------------------------------------------------------
// ONE badge system, two roles. A LIVE vendor hit names the vendor it came from (amber:
// off-library, often shipping no model); a local hit reads "library" (accent). The old
// code drew a pill only for live results, so a local row carried no source marker at all
// and the two kinds were told apart only by what was missing.
std::string sourceBadgeLabel(const PartsItem& item) {
    if (item.source != "live") return "library";
    return item.vendor.empty() ? "live" : item.vendor;
}

const float* sourceBadgeRole(const PartsItem& item) {
    return item.source == "live" ? palette.statusWarn : palette.accent;
}

// A vendor name is unbounded where the old fixed "live" tag could not overflow, so the
// label is shrunk (whole UTF-8 code points) until the PILL — not the text — fits `maxW`.
std::string fitBadgeLabel(std::string s, float maxW) {
    if (badgeW(s.c_str()) <= maxW) return s;
    while (s.size() > 1 && badgeW((s + "\xe2\x80\xa6").c_str()) > maxW) {
        do { s.pop_back(); } while (!s.empty() &&
                                    ((unsigned char)s.back() & 0xC0) == 0x80);
    }
    s += "\xe2\x80\xa6";
    return s;
}

// One unbounded line of compact action buttons from the cursor — LIST view only, where the
// table cell does the clipping. The grid does NOT use this: it draws one primary pill plus a
// "…" overflow popup so its hover band can never grow past a single text line.
void actionsLayout(AppState& st, const PartsItem& item) {
    ItemAction acts[kMaxItemActions];
    const int n = collectActions(item, acts);
    if (n == 0) return;

    const float btnH = ImGui::GetTextLineHeight();     // compact: no vertical frame padding
    const float fpX  = ImGui::GetStyle().FramePadding.x;
    const float gap  = ui::metrics::U(1);
    ImGui::PushStyleVar(ImGuiStyleVar_FramePadding, ImVec2(fpX, 0.0f));
    ImGui::PushStyleVar(ImGuiStyleVar_FrameRounding, 4.0f * ui::uiScale);
    ImGui::PushStyleVar(ImGuiStyleVar_ButtonTextAlign, ImVec2(0.0f, 0.5f));
    for (int i = 0; i < n; ++i) {
        if (i > 0) ImGui::SameLine(0.0f, gap);
        const float w = ImGui::CalcTextSize(acts[i].label).x + fpX * 2.0f;
        if (ImGui::Button(acts[i].label, ImVec2(w, btnH)))
            runAction(st, item, acts[i].kind);
        if (acts[i].kind == ActKind::GenerateModel && ImGui::IsItemHovered())
            ImGui::SetTooltip("%s", kGenModelTip);
    }
    ImGui::PopStyleVar(3);
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

// Indeterminate progress bar for the "more results are still landing" state: 2dp of accent
// riding at the top of the results region. There is no completion fraction to report (each
// vendor answers independently), so the bar reports MOTION — the point is that the rows
// already on screen are live and more are coming, not a percentage.
static void resultsProgressBar() {
    const float  sc = ui::uiScale;
    const float  h  = std::max(2.0f * sc, 2.0f);
    const ImVec2 p0 = ImGui::GetCursorScreenPos();
    const float  w  = std::max(ImGui::GetContentRegionAvail().x, 1.0f);
    const ImVec2 p1(p0.x + w, p0.y + h);
    ImDrawList*  dl = ImGui::GetWindowDrawList();
    const float  r  = h * 0.5f;

    float track[4], head[4];
    Palette::withAlpha(palette.accent, track, 0.16f);
    Palette::withAlpha(palette.accent, head,  0.92f);
    dl->AddRectFilled(p0, p1, Palette::toU32(track), r);

    const float seg  = std::max(56.0f * sc, w * 0.30f);
    const float span = w + seg;   // the head enters off the left and exits off the right
    const float x0   = p0.x - seg + std::fmod((float)ImGui::GetTime() * 0.9f, 1.0f) * span;
    dl->PushClipRect(p0, p1, true);
    dl->AddRectFilled(ImVec2(x0, p0.y), ImVec2(x0 + seg, p1.y), Palette::toU32(head), r);
    dl->PopClipRect();

    ImGui::Dummy(ImVec2(w, h + 2.0f * sc));   // reserve the strip: results start below it
}

// Searching / error / empty preamble, shared by both renderers so a backend failure can
// never be misreported as "No results" in one view but not the other. Returns true when it
// painted a state and the caller must return without drawing results; each renderer passes
// its own empty-state copy.
//
// PROGRESSIVE LOAD: a search still in flight does NOT block a result set we already hold.
// The old code returned true on `searching` and threw the whole list away for a spinner, so
// a streaming vendor fan-out showed nothing until the slowest vendor answered — the results
// were in memory and the user was watching a spinner. The blocking states are now reserved
// for "we have literally nothing to show"; with rows in hand the only chrome is the slim
// bar above them, and the rows stay fully interactive (hover, tooltip, actions, insert)
// while the rest arrive.
static bool resultsPreamble(const char* noResultsSub, const char* promptSub) {
    if (!S().results.empty() && (S().searching || S().liveSearching)) {
        resultsProgressBar();
        return false;
    }
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
        // Domain colour: the SAME hue the grid card and the category tree use, so a
        // fastener reads as a fastener in every view. Painted from the row-spanning
        // Selectable's rect (SpanAllColumns → the item rect already covers every column)
        // before any cell content draws, so the wash can never sit on top of the text.
        const float* tint = categoryTint(item.domain);
        ui::coloredRowWash(dl, rMin, rMax, 5.0f, tint, hovered ? 0.26f : 0.11f,
                           /*selected=*/false);
        if (hovered) {
            // Hover reads as the brighter wash above + the border treatment; the old flat
            // treeHover fill is gone — a grey slab over the hue undid the colour coding.
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
        } else if (S().thumbBusy.count(item.id) != 0) {
            // Loading, not missing. A shimmering tinted block says "an image is coming";
            // the static glyph below says "this part has no photo". Without the split, a
            // row mid-fetch is indistinguishable from a row that will never have one.
            const float ph = 0.5f + 0.5f * std::sin((float)ImGui::GetTime() * 2.0f);
            ui::glassCardWash(dl, ImVec2(rowPos.x, rowPos.y + (rowH - iconSz) * 0.5f),
                              ImVec2(rowPos.x + iconSz, rowPos.y + (rowH + iconSz) * 0.5f),
                              4.0f, tint, 0.14f + 0.20f * ph, 0.55f);
        } else {
            partIconDraw(dl, ImVec2(rowPos.x + iconSz * 0.5f, rowPos.y + rowH * 0.5f),
                         iconSz, item.domain);
        }
        ImGui::SetCursorScreenPos(ImVec2(rowPos.x + iconSz + 6.0f * sc,
                                         rowPos.y + (rowH - ImGui::GetTextLineHeight()) * 0.5f));
        if (item.downloaded) {
            badgeInline("saved", palette.statusOk);
            ImGui::SameLine();
        }
        // Source pill: the vendor's name (amber) for a live hit, "library" (accent) for a
        // local one. Capped so a long vendor can never shove the part name out of the cell.
        badgeInline(fitBadgeLabel(sourceBadgeLabel(item), 96.0f * sc).c_str(),
                    sourceBadgeRole(item));
        ImGui::SameLine();
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
        if (item.priceUsd > 0.0) {
            // The one number a buyer scans a results table for — it should not be the same
            // tone as the part number three columns to its left.
            ImGui::PushStyleColor(ImGuiCol_Text,
                ImVec4(palette.accent[0], palette.accent[1], palette.accent[2], 1.0f));
            ImGui::Text("$%.2f/%s", item.priceUsd, item.unit.c_str());
            ImGui::PopStyleColor();
        } else {
            ImGui::TextDisabled("—");
        }

        ImGui::TableSetColumnIndex(5);
        if (hovered) {
            ImGui::PushStyleColor(ImGuiCol_Button,
                ImVec4(palette.button[0], palette.button[1],
                       palette.button[2], palette.button[3]));
            actionsLayout(st, item);
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
// number, the price and the hover action row all start on ONE content line, and the hover
// band is exactly one text line so it can never cover the thumbnail it annotates.
void drawResultsGrid(AppState& st) {
    const float sc    = ui::uiScale;
    const float pad   = ui::metrics::partsPadX();
    const float imgH  = 88.0f * sc;
    const float gap   = 8.0f * sc;
    const float cardRound = 8.0f;      // logical corner; the helpers scale it themselves

    // The card height is DERIVED from what a card actually holds — preview band, title in
    // the medium face, part number, price, and the one-line hover action band — instead of a
    // 168 px guess that the band then had to overlap. A hovered card must never eat its own
    // price line, so the band's strip is reserved in the height rather than borrowed from
    // the text. Every term below is repeated verbatim by the per-card painter; change both.
    const float lineH    = ImGui::GetTextLineHeight();
    float titleH = lineH;
    if (S().fonts && S().fonts->medium) {
        ImGui::PushFont(S().fonts->medium);
        titleH = ImGui::GetTextLineHeight();
        ImGui::PopFont();
    }
    const float bandPadY = 4.0f * sc;
    const float bandH    = lineH + 2.0f * bandPadY;
    const float textH    = 6.0f * sc + titleH + 2.0f * sc + lineH + 2.0f * sc + lineH +
                           6.0f * sc;
    const float cardH    = imgH + textH + bandH;

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
    // lineH / titleH / bandH computed above — the card height depends on them.
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
        // The overflow popup floats OUTSIDE the card, so plain rect-hover would drop the band
        // (and with it the BeginPopup call) the instant the pointer left the card to reach it.
        const bool hov = ImGui::IsMouseHoveringRect(p0, p1) || ImGui::IsPopupOpen("##more");

        // Resting frosted surface + the DOMAIN's colour wash on top, so the grid reads as
        // colour-coded groups instead of 40 identical slate tiles. Hover brightens the same
        // wash (never a different surface) and adds the border.
        ui::glassRowBackground(dl, p0, p1, cardRound, false, false, 0.20f);
        const float* tint = categoryTint(item.domain);
        ui::glassCardWash(dl, p0, p1, cardRound, tint, hov ? 0.22f : 0.12f);
        borderCardRect(dl, p0, p1, /*selected=*/false, hov, cardRound);

        dl->PushClipRect(p0, p1, true);

        // ---- preview band: backdrop, thumbnail (or domain glyph), status markers ----
        // The backdrop is what stops a transparent product PNG and the flat vector fallback
        // from floating on the card's glass with no defined picture area; the hairline under
        // it separates the picture from the text block instead of leaving them one soft blur.
        const ImVec2 bandBR(p1.x, p0.y + imgH);
        float previewBg[4];
        Palette::withAlpha(palette.frameBg, previewBg, 0.5f);
        dl->AddRectFilled(p0, bandBR, Palette::toU32(previewBg), cardRound * sc,
                          ImDrawFlags_RoundCornersTop);
        dl->AddLine(ImVec2(p0.x, bandBR.y - 0.5f), ImVec2(p1.x, bandBR.y - 0.5f),
                    Palette::toU32(palette.border), ui::metrics::stroke(1.0f));

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
        } else if (S().thumbBusy.count(item.id) != 0) {
            // Shimmering placeholder while the thumbnail request is in flight — same
            // loading-vs-imageless distinction the list row makes, so switching views
            // never changes what a card means. thumbFailed items fall through to the glyph.
            const float ph = 0.5f + 0.5f * std::sin((float)ImGui::GetTime() * 2.0f);
            ui::glassCardWash(dl, ImVec2(cx, p0.y + pad * 0.5f),
                              ImVec2(cx + contentW, p0.y + imgH - pad * 0.5f),
                              6.0f, tint, 0.12f + 0.22f * ph, 0.45f);
        } else {
            partIconDraw(dl, ImVec2(cx + contentW * 0.5f, p0.y + imgH * 0.5f + 4.0f * sc),
                         std::min(52.0f * sc, contentW), item.domain);
        }

        // Status rides ON the preview band, inside the card clip: "downloaded" as a check
        // glyph top-left, "live vendor result" as a badge top-right. Both used to be ASCII
        // tags welded to the price line, which left half that line to a status marker.
        const float markY = p0.y + 6.0f * sc;
        if (item.downloaded)
            ui::drawIconGlass(dl, "check",
                              ImVec2(cx + badgeH() * 0.5f, markY + badgeH() * 0.5f),
                              badgeH(), Palette::toU32(palette.statusOk), 0);
        // Source pill, one system for both kinds (see sourceBadgeLabel): live hits name
        // their VENDOR in amber, local hits read "library" in accent.
        {
            const std::string sb = fitBadgeLabel(sourceBadgeLabel(item), contentW * 0.72f);
            drawBadge(dl, ImVec2(p1.x - pad - badgeW(sb.c_str()), markY), sb.c_str(),
                      sourceBadgeRole(item));
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
        // Title in the medium face so a card has a headline instead of three identical rows of
        // small text. contentText MEASURES with the live font (it ellipsizes against contentW)
        // and paints through the drawlist, so the push has to wrap the whole call — and the
        // next line advances by the TITLE's height, not the body height, or it overlaps.
        ImFont* titleFont = S().fonts ? S().fonts->medium : nullptr;
        if (titleFont) ImGui::PushFont(titleFont);
        // titleH is hoisted (the card height is derived from it) — just push the face here.
        contentText(ty, item.name.c_str(), ImGui::GetColorU32(ImGuiCol_Text));
        if (titleFont) ImGui::PopFont();
        ty += titleH + 2.0f * sc;
        if (!item.partNumber.empty()) {
            contentText(ty, item.partNumber.c_str(), Palette::toU32(palette.textDim));
            ty += lineH + 2.0f * sc;
        }
        // The "live" marker moved to the preview badge, so this line is just the price.
        char pbuf[48];
        if (item.priceUsd > 0.0) std::snprintf(pbuf, sizeof(pbuf), "$%.2f", item.priceUsd);
        else                     std::snprintf(pbuf, sizeof(pbuf), "%s", "—");
        contentText(ty, pbuf, item.priceUsd > 0.0 ? Palette::toU32(palette.accent)
                                                  : Palette::toU32(palette.textDisabled));

        dl->PopClipRect();

        // ---- hover actions: a ONE-LINE band INSIDE the card rect ----------------------
        // The band used to MEASURE a wrapped block of up to four full-width buttons and grow
        // upward to fit them, which swallowed the price, the title and most of the thumbnail:
        // hovering a card erased the card. It is now pinned to one text line — the primary
        // action (collectActions orders Download before Insert) as an accent pill, plus a
        // compact "…" button that opens the remaining actions in a popup.
        if (hov) {
            ItemAction acts[kMaxItemActions];
            const int n = collectActions(item, acts);
            if (n > 0) {
                // bandPadY / bandH are hoisted: the card RESERVES this strip, so the band
                // sits under the price line instead of covering it.
                const float btnH    = lineH;
                const float bandTop = p1.y - bandH;
                const ImVec2 b0(p0.x, bandTop), b1(p1.x, p1.y);
                // Opaque base BEFORE the glass tint: glassBg is translucent (alpha .87), so
                // the card's own price line showed through the band as overlapping text.
                dl->AddRectFilled(b0, b1, Palette::toU32(palette.panelBg), cardRound * sc,
                                  ImDrawFlags_RoundCornersBottom);
                dl->AddRectFilled(b0, b1, Palette::toU32(palette.glassBg), cardRound * sc,
                                  ImDrawFlags_RoundCornersBottom);

                const float fpX   = ImGui::GetStyle().FramePadding.x;
                const float gapX  = ui::metrics::U(1);
                // Sized from its own label but never past 62% of the content width, so the
                // "…" button always has room on the same line no matter how long the label.
                const float primW = std::min(ImGui::CalcTextSize(acts[0].label).x + fpX * 2.0f,
                                             contentW * 0.62f);

                ImGui::PushClipRect(b0, b1, true);
                ImGui::PushStyleVar(ImGuiStyleVar_FramePadding, ImVec2(fpX, 0.0f));
                ImGui::PushStyleVar(ImGuiStyleVar_FrameRounding, 4.0f * sc);
                ImGui::SetCursorScreenPos(ImVec2(cx, bandTop + bandPadY));
                // Primary = the card's call to action, so it carries the accent fill with
                // max-contrast ink (readable in BOTH themes); overflow stays neutral.
                const ImVec4 acc(palette.accent[0], palette.accent[1], palette.accent[2], 1.0f);
                ImGui::PushStyleColor(ImGuiCol_Button, ImVec4(acc.x, acc.y, acc.z, 0.85f));
                ImGui::PushStyleColor(ImGuiCol_ButtonHovered, acc);
                ImGui::PushStyleColor(ImGuiCol_ButtonActive,
                    ImVec4(palette.accentDim[0], palette.accentDim[1],
                           palette.accentDim[2], 1.0f));
                ImGui::PushStyleColor(ImGuiCol_Text, ui::maxContrastTextOn(acc));
                if (ImGui::Button(acts[0].label, ImVec2(primW, btnH)))
                    runAction(st, item, acts[0].kind);
                ImGui::PopStyleColor(4);
                if (n > 1) {
                    ImGui::SameLine(0.0f, gapX);
                    // Square, with its OWN zero horizontal padding: inside a button only one
                    // text line wide the row's frame padding inverts the inner rect and shoves
                    // the ellipsis onto the right edge instead of centring it.
                    ImGui::PushStyleVar(ImGuiStyleVar_FramePadding, ImVec2(0.0f, 0.0f));
                    ImGui::PushStyleColor(ImGuiCol_Button,
                        ImVec4(palette.button[0], palette.button[1],
                               palette.button[2], palette.button[3]));
                    if (ImGui::Button("\xe2\x80\xa6##more", ImVec2(btnH, btnH)))
                        ImGui::OpenPopup("##more");
                    ImGui::PopStyleColor();
                    ImGui::PopStyleVar();
                    if (ImGui::IsItemHovered()) ImGui::SetTooltip("More actions");
                }
                ImGui::PopStyleVar(2);
                ImGui::PopClipRect();

                // Popup AFTER the clip scope: it is its own window and must inherit neither
                // the band's clip rect nor the compact button styling.
                if (n > 1 && ImGui::BeginPopup("##more")) {
                    for (int a = 1; a < n; ++a) {
                        if (ImGui::Selectable(acts[a].label))
                            runAction(st, item, acts[a].kind);
                        if (acts[a].kind == ActKind::GenerateModel && ImGui::IsItemHovered())
                            ImGui::SetTooltip("%s", kGenModelTip);
                    }
                    ImGui::EndPopup();
                }
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
