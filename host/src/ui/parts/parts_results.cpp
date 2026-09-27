// Parts Library — results (list table + grid cards) and per-item actions.
// GUI V2 phase A: the list is a real ImGui table (Name | Vendor | Part# | Material | Price)
// with an 18px domain icon before the name; hover shows the border treatment + the item's
// actions. Column widths are BUDGETED from the pane width every frame (see drawResultsList)
// and every cell's text is ellipsized to its cell — a table cell only hard-clips.
// Status ("downloaded", "live vendor result") is carried by drawn badges/glyphs — the ASCII
// bracket tags they replace read as debug output and shoved the real text sideways.
#include <algorithm>
#include <cfloat>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <string>
#include <string_view>

#include "imgui.h"
#include "imgui_internal.h"   // TablePushBackgroundChannel — the row wash spans every column

#include "core/app_state.h"
#include "ui_theme.h"
#include "ui/icons.h"      // ui::drawIconGlass — atlas glyphs on the card preview band
#include "ui/metrics.h"    // ui::metrics::partsPadX — the ONE horizontal inset of this workspace
#include "ui/widgets/kit.h"    // ui::kit — the app-wide progress painter
#include "ui/tesseract.h"  // ui::drawTesseractSpinner — the brand mark IS the busy indicator

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

bool isCommunityCad(const PartsItem& item) {
    return item.id.rfind("catalog:printables::", 0) == 0 ||
           item.id.rfind("catalog:makerworld::", 0) == 0;
}

int collectActions(const PartsItem& item, ItemAction* out) {
    int n = 0;
    const bool isCatalog = item.id.rfind("catalog:", 0) == 0;
    const bool community = isCommunityCad(item);
    if (community) {
        out[n++] = {"Download & import", ActKind::Download};
    } else {
        if (isCatalog && !item.downloaded) out[n++] = {"Download", ActKind::Download};
        if (item.placeable) out[n++] = {"Insert", ActKind::Insert};
        if (item.source == "live") out[n++] = {"Reverse-engineer", ActKind::ReverseEngineer};
        if (!item.placeable) out[n++] = {"Generate 3D model", ActKind::GenerateModel};
    }
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

// A vendor favicon riding INSIDE the source pill. 14dp square, 4dp of air before the label;
// a pill without one keeps exactly the geometry it always had, so an uncached vendor renders
// pixel-identical to before. Measurement and painting share these helpers — if badgeW and
// drawBadge disagreed about the logo the pill would be mis-sized every frame.
float badgeLogoSz() { return 14.0f * ui::uiScale; }
float badgeLogoAdvance(bool withLogo) {
    return withLogo ? badgeLogoSz() + 4.0f * ui::uiScale : 0.0f;
}

// Air inside the pill, per side (the 10dp the old single-term width formula folded together).
float badgePadX() { return 5.0f * ui::uiScale; }

float badgeW(const char* label, bool withLogo = false) {
    ImFont* f = ImGui::GetFont();
    return f->CalcTextSizeA(badgeTextSize(), FLT_MAX, 0.0f, label).x +
           2.0f * badgePadX() + badgeLogoAdvance(withLogo);
}

void drawBadge(ImDrawList* dl, ImVec2 p0, const char* label, const float role[4],
               unsigned int logoTex = 0) {
    const float fs  = badgeTextSize();
    const float px  = badgePadX();
    const float adv = badgeLogoAdvance(logoTex != 0);
    ImFont* f = ImGui::GetFont();
    const ImVec2 ts = f->CalcTextSizeA(fs, FLT_MAX, 0.0f, label);
    const ImVec2 p1(p0.x + ts.x + 2.0f * px + adv, p0.y + badgeH());
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
    if (logoTex) {
        const float lz = badgeLogoSz();
        const ImVec2 l0(p0.x + px, p0.y + (badgeH() - lz) * 0.5f);
        dl->AddImage((ImTextureID)(intptr_t)logoTex, l0, ImVec2(l0.x + lz, l0.y + lz));
    }
    dl->AddText(f, fs, ImVec2(p0.x + px + adv, p0.y + (p1.y - p0.y - ts.y) * 0.5f),
                Palette::toU32(role), label);
}

// Layout-participating badge for the list rows: reserves its own footprint with a Dummy so
// the SameLine flow (badges, then the name) is unchanged, centred on the current text line.
void badgeInline(const char* label, const float role[4], unsigned int logoTex = 0) {
    const ImVec2 cur = ImGui::GetCursorScreenPos();
    const float  lh  = ImGui::GetTextLineHeight();
    drawBadge(ImGui::GetWindowDrawList(),
              ImVec2(cur.x, cur.y + (lh - badgeH()) * 0.5f), label, role, logoTex);
    ImGui::Dummy(ImVec2(badgeW(label, logoTex != 0), lh));
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

// A live result's id is "catalog:<site>::<url>", so the vendor's site key is the token
// between the prefix and the separator. Anything else — a local library row — has no vendor
// site and keeps the plain text pill.
std::string_view siteKeyFor(const PartsItem& item) {
    constexpr char   kPfx[] = "catalog:";
    constexpr size_t kPfxN  = sizeof(kPfx) - 1;
    if (item.id.compare(0, kPfxN, kPfx) != 0) return {};
    const size_t sep = item.id.find("::", kPfxN);
    if (sep == std::string::npos || sep == kPfxN) return {};
    return std::string_view(item.id).substr(kPfxN, sep - kPfxN);
}

// DRAW-ONLY favicon lookup. The prefetch that populates "vendorlogo:<site>" lives with the
// vendor list, so a miss here is normal (not fetched yet, or a local row) and simply yields
// the text-only pill this file has always drawn. The lastUse touch is what keeps a logo the
// user can currently see out of the thumb LRU's eviction set.
unsigned int vendorLogoTex(const PartsItem& item) {
    const std::string_view site = siteKeyFor(item);
    if (site.empty()) return 0;
    // Reused key buffer: this runs once per visible row per frame and S().thumbs is keyed by
    // std::string (no heterogeneous lookup), so a fresh temporary would cost an allocation
    // per row per frame for nothing. ImGui draws on one thread.
    static std::string key;
    key.assign("vendorlogo:").append(site);
    auto it = S().thumbs.find(key);
    if (it == S().thumbs.end() || !it->second.tex) return 0;
    it->second.lastUse = ImGui::GetTime();
    return it->second.tex;
}

// A vendor name is unbounded where the old fixed "live" tag could not overflow, so the
// label is shrunk (whole UTF-8 code points) until the PILL — logo included, not just the
// text — fits `maxW`.
std::string fitBadgeLabel(std::string s, float maxW, bool withLogo = false) {
    if (badgeW(s.c_str(), withLogo) <= maxW) return s;
    while (s.size() > 1 && badgeW((s + "\xe2\x80\xa6").c_str(), withLogo) > maxW) {
        do { s.pop_back(); } while (!s.empty() &&
                                    ((unsigned char)s.back() & 0xC0) == 0x80);
    }
    s += "\xe2\x80\xa6";
    return s;
}

// ONE line of compact action buttons from the cursor — LIST view only. The line is BUDGETED
// against the cell's remaining width: the table's PadOuterX already holds partsPadX() back
// from the row's right edge, so laying out inside GetContentRegionAvail() is exactly what
// keeps the last button that far clear of it. Anything that does not fit moves into a "…"
// popup instead of being drawn half-clipped off the row — which is what the unbounded old
// line did with four actions in a 140dp column.
void actionsLayout(AppState& st, const PartsItem& item) {
    ItemAction acts[kMaxItemActions];
    const int n = collectActions(item, acts);
    if (n == 0) return;

    const float btnH  = ImGui::GetTextLineHeight();    // compact: no vertical frame padding
    const float fpX   = ImGui::GetStyle().FramePadding.x;
    const float gap   = ui::metrics::U(1);
    const float avail = ImGui::GetContentRegionAvail().x;
    const float moreW = btnH;                          // square overflow button

    float w[kMaxItemActions];
    for (int i = 0; i < n; ++i) w[i] = ImGui::CalcTextSize(acts[i].label).x + fpX * 2.0f;
    int   shown = 0;
    float used  = 0.0f;
    for (int i = 0; i < n; ++i) {
        const float step = (shown > 0 ? gap : 0.0f) + w[i];
        const float tail = (i + 1 < n) ? gap + moreW : 0.0f;   // room for "…" if any remain
        if (used + step + tail > avail) break;
        used += step;
        ++shown;
    }
    if (shown == 0 && moreW > avail) return;   // nothing fits at all: draw nothing

    ImGui::PushStyleVar(ImGuiStyleVar_FramePadding, ImVec2(fpX, 0.0f));
    ImGui::PushStyleVar(ImGuiStyleVar_FrameRounding, 4.0f * ui::uiScale);
    ImGui::PushStyleVar(ImGuiStyleVar_ButtonTextAlign, ImVec2(0.0f, 0.5f));
    for (int i = 0; i < shown; ++i) {
        if (i > 0) ImGui::SameLine(0.0f, gap);
        if (ImGui::Button(acts[i].label, ImVec2(w[i], btnH)))
            runAction(st, item, acts[i].kind);
        if (acts[i].kind == ActKind::GenerateModel && ImGui::IsItemHovered())
            ImGui::SetTooltip("%s", kGenModelTip);
    }
    if (shown < n) {
        if (shown > 0) ImGui::SameLine(0.0f, gap);
        // Square, with its OWN zero horizontal padding and a centred label: inside a button
        // only one text line wide the row's frame padding inverts the inner rect and shoves
        // the ellipsis onto the right edge instead of centring it.
        ImGui::PushStyleVar(ImGuiStyleVar_FramePadding, ImVec2(0.0f, 0.0f));
        ImGui::PushStyleVar(ImGuiStyleVar_ButtonTextAlign, ImVec2(0.5f, 0.5f));
        const bool clicked = ImGui::Button("\xe2\x80\xa6##rowmore", ImVec2(moreW, btnH));
        ImGui::PopStyleVar(2);
        if (clicked) ImGui::OpenPopup("##rowmore");
        if (ImGui::IsItemHovered()) ImGui::SetTooltip("More actions");
    }
    ImGui::PopStyleVar(3);

    // Popup AFTER the compact styling is popped: it is its own window and must inherit none
    // of it. The row keeps `hovered` true while it is open (see drawResultsList).
    if (shown < n && ImGui::BeginPopup("##rowmore")) {
        for (int i = shown; i < n; ++i) {
            if (ImGui::Selectable(acts[i].label))
                runAction(st, item, acts[i].kind);
            if (acts[i].kind == ActKind::GenerateModel && ImGui::IsItemHovered())
                ImGui::SetTooltip("%s", kGenModelTip);
        }
        ImGui::EndPopup();
    }
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
    if (!item.vendor.empty())     ImGui::Text("Vendor: %s", item.vendor.c_str());
    if (!item.fileName.empty()) ImGui::Text("CAD file to download: %s", item.fileName.c_str());
    else if (!item.partNumber.empty()) ImGui::Text("Part #: %s", item.partNumber.c_str());
    if (!item.material.empty()) ImGui::Text("Material: %s", item.material.c_str());
    if (!item.standard.empty()) ImGui::Text("Standard: %s", item.standard.c_str());
    if (!item.sizes.empty()) {
        std::string sizes = "Available sizes: ";
        for (const auto& size : item.sizes) {
            if (sizes.size() > 17) sizes += " · ";
            sizes += size;
        }
        ImGui::TextWrapped("%s", sizes.c_str());
    }
    if (!item.license.empty())  ImGui::TextDisabled("License: %s", item.license.c_str());
    if (!item.datasheet.empty()) ImGui::TextDisabled("Datasheet: %s", item.datasheet.c_str());
    ImGui::TextDisabled("%s", item.source == "live"
                              ? (item.domain == "catalog" ? "Community / vendor CAD result" : "Live vendor result")
                              : "Local standard-parts library");
    ImGui::EndTooltip();
}

static void itemTooltip(const PartsItem& item) {
    itemTooltipAt(item, ImGui::IsItemHovered(ImGuiHoveredFlags_DelayNormal));
}

// Indeterminate progress bar for the "more results are still landing" state: 2dp of accent riding
// at the top of the results region. There is no completion fraction to report (each vendor answers
// independently), so the bar reports MOTION — an out-of-range fraction is exactly how ui::kit
// spells that, and its marquee drives the frame pump instead of stuttering at the 20 Hz idle rate.
static void resultsProgressBar() {
    const float h = std::max(2.0f * ui::uiScale, 2.0f);
    // The results pane's scroll child carries the copilot pill scrollbar on its right edge, so the
    // bar stops a lane short of the content region instead of running under it. A zero-padding,
    // transparent child is how that inset is handed to a shared painter that sizes itself from the
    // content region.
    const float w = std::max(ImGui::GetContentRegionAvail().x -
                                 ui::metrics::px(ui::kChatScrollbarLane), 1.0f);
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(0.0f, 0.0f));
    ImGui::PushStyleColor(ImGuiCol_ChildBg, ImVec4(0, 0, 0, 0));
    ImGui::BeginChild("##parts_results_progress", ImVec2(w, h), ImGuiChildFlags_None,
                      ImGuiWindowFlags_NoScrollbar | ImGuiWindowFlags_NoScrollWithMouse);
    ui::kit::progressBar(/*frac=*/-1.0f, /*caption=*/nullptr, ui::kit::Tone::Accent, h);
    ImGui::EndChild();
    ImGui::PopStyleColor();
    ImGui::PopStyleVar();
    ImGui::Dummy(ImVec2(w, 2.0f * ui::uiScale));   // results start below the strip
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
// Queue the rows the user can see before the surrounding prefetch margin.  Re-prioritising
// only touches requests that are still queued: already-dispatched offscreen work is allowed
// to finish, while a fast scroll does not leave the new viewport behind stale queued rows.
static void requestThumbWindow(AppState& st, int visibleStart, int visibleEnd, int marginItems) {
    const int count = static_cast<int>(S().results.size());
    visibleStart = std::max(0, std::min(visibleStart, count));
    visibleEnd   = std::max(visibleStart, std::min(visibleEnd, count));

    for (int i = visibleStart; i < visibleEnd; ++i)
        requestThumb(st, S().results[i]);
    // prioritizeThumb moves one item to the front, so visit bottom-to-top to retain visual order.
    for (int i = visibleEnd; i-- > visibleStart;)
        prioritizeThumb(S().results[i].id);

    marginItems = std::max(1, marginItems);
    const int prefetchStart = std::max(0, visibleStart - marginItems);
    const int prefetchEnd   = std::min(count, visibleEnd + marginItems);
    for (int i = prefetchStart; i < visibleStart; ++i)
        requestThumb(st, S().results[i]);
    for (int i = visibleEnd; i < prefetchEnd; ++i)
        requestThumb(st, S().results[i]);
}

// Price cell text. ONE formatter for the column measure pass and the draw, so a Price column
// sized from it always fits exactly what is painted.
static void priceLabel(const PartsItem& item, char* buf, size_t n) {
    if (item.priceUsd > 0.0)
        std::snprintf(buf, n, "$%.2f/%s", item.priceUsd, item.unit.c_str());
    else
        std::snprintf(buf, n, "%s", "\xe2\x80\x94");   // em dash: "no price"
}

// Natural width of each content-sized list column: the widest cell over the WHOLE result
// set, header included, so a column does not breathe as rows scroll through the clipper.
// Re-measured only when the set or the font changes — the budget reads it every frame. The
// caps stop one pathological vendor/material/part string from starving Name; a capped
// column ellipsizes its outliers instead.
struct ListColumnWidths { float vendor = 0.0f, part = 0.0f, material = 0.0f, price = 0.0f; };

static const ListColumnWidths& listColumnWidths() {
    static ListColumnWidths w;
    static size_t      n      = SIZE_MAX;
    static float       fontSz = -1.0f;
    static std::string firstId, lastId;
    const auto& rs = S().results;
    const float fs = ImGui::GetFontSize();
    if (rs.size() == n && fs == fontSz &&
        (rs.empty() || (rs.front().id == firstId && rs.back().id == lastId)))
        return w;
    n      = rs.size();
    fontSz = fs;
    firstId.assign(rs.empty() ? std::string_view() : std::string_view(rs.front().id));
    lastId.assign(rs.empty() ? std::string_view() : std::string_view(rs.back().id));

    auto textW = [](const std::string& s) { return ImGui::CalcTextSize(s.c_str()).x; };
    w.vendor   = ImGui::CalcTextSize("Vendor").x;
    w.part     = ImGui::CalcTextSize("Part # / CAD file").x;
    w.material = ImGui::CalcTextSize("Material / License").x;
    w.price    = ImGui::CalcTextSize("Price").x;
    char buf[64];
    for (const auto& it : rs) {
        w.vendor   = std::max(w.vendor,   textW(it.vendor));
        w.part     = std::max(w.part,     textW(it.partNumber));
        w.material = std::max(w.material, textW(isCommunityCad(it) ? it.license : it.material));
        priceLabel(it, buf, sizeof(buf));
        w.price    = std::max(w.price, ImGui::CalcTextSize(buf).x);
    }
    const float sc = ui::uiScale;
    w.vendor   = std::ceil(std::min(w.vendor,   130.0f * sc));
    w.part     = std::ceil(std::min(w.part,     180.0f * sc));
    w.material = std::ceil(std::min(w.material, 150.0f * sc));
    w.price    = std::ceil(std::min(w.price,    110.0f * sc));
    return w;
}

// A table cell only hard-clips ("Socket Head Cap Scre"); this ellipsizes `text` to what is
// left of the current cell instead. Allocates only when the text actually overflows.
static void cellText(const char* text) {
    const float avail = ImGui::GetContentRegionAvail().x;
    if (ImGui::CalcTextSize(text).x <= avail) {
        ImGui::TextUnformatted(text);
        return;
    }
    ImGui::TextUnformatted(ui::kit::elide(text, std::max(1.0f, avail)).c_str());
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

    // The results render into the workspace's scroll child, whose right edge carries the
    // copilot's thick pill scrollbar (ui::kChatScrollbar). The row highlight spans the WHOLE
    // table (SpanAllColumns), so unless the table itself stops a lane short, every row pill —
    // and the action buttons in the last column — slides under the bar.
    const float laneW  = ui::metrics::px(ui::kChatScrollbarLane);
    const float tableW = std::max(ImGui::GetContentRegionAvail().x - laneW,
                                  ui::metrics::U(12));

    // Column budget, recomputed from the pane width every frame. Fixed pixel columns kept
    // their full width however little was left, so the stretch Name column — the one that
    // says what the part IS — was crushed to "Nar" at 1.5x. Columns are now granted in
    // priority order out of what the pane actually has:
    //   Name (readable minimum) + the actions' "…" button  →  Price  →  Part #
    //   →  Name up to a comfortable width  →  the inline action buttons  →  Vendor → Material
    // Part # / Price / Vendor / Material are sized to their content (listColumnWidths); Name
    // stretches over everything left, so a wide pane widens the name, not the gutters. A
    // column that does not fit is disabled outright; its value stays in the row tooltip.
    const ListColumnWidths& cw = listColumnWidths();
    // PadOuterX chrome is exactly 2*CellPadding.x per VISIBLE column (outer padding both
    // ends + inner spacing between each pair).
    const float colChrome   = 2.0f * ImGui::GetStyle().CellPadding.x;
    const float nameMin     = 160.0f * sc;
    const float nameComfort = 320.0f * sc;
    // actionsLayout needs one square line-height "…" to reach every action from its popup;
    // anything wider shows the first actions inline.
    const float actMin  = std::ceil(ImGui::GetTextLineHeight()) + ui::metrics::px(1.0f);
    const float actFull = 140.0f * sc;
    float room = tableW - (nameMin + colChrome) - (actMin + colChrome);
    auto grant = [&room, colChrome](float w) {
        if (room < w + colChrome) return false;
        room -= w + colChrome;
        return true;
    };
    const bool showPrice = grant(cw.price);
    const bool showPart  = grant(cw.part);
    room -= std::min(std::max(room, 0.0f), nameComfort - nameMin);   // Name's comfort share
    const float actGrow = std::clamp(room, 0.0f, actFull - actMin);
    room -= actGrow;
    const float wAct = actMin + actGrow;
    const bool showVendor   = grant(cw.vendor);
    const bool showMaterial = grant(cw.material);
    auto fixedCol = [](bool shown) -> ImGuiTableColumnFlags {
        return ImGuiTableColumnFlags_WidthFixed |
               (shown ? ImGuiTableColumnFlags_None : ImGuiTableColumnFlags_Disabled);
    };

    const ImGuiTableFlags tblFlags = ImGuiTableFlags_SizingFixedFit |
                                     ImGuiTableFlags_NoBordersInBody |
                                     ImGuiTableFlags_PadOuterX;
    if (!ImGui::BeginTable("##parts_tbl", 6, tblFlags, ImVec2(tableW, 0.0f))) {
        ImGui::PopStyleVar();
        return;
    }
    ImGui::TableSetupColumn("Name",     ImGuiTableColumnFlags_WidthStretch);
    ImGui::TableSetupColumn("Vendor",   fixedCol(showVendor),   cw.vendor);
    ImGui::TableSetupColumn("Part # / CAD file", fixedCol(showPart), cw.part);
    ImGui::TableSetupColumn("Material / License", fixedCol(showMaterial), cw.material);
    ImGui::TableSetupColumn("Price",    fixedCol(showPrice),    cw.price);
    ImGui::TableSetupColumn("##act",    ImGuiTableColumnFlags_WidthFixed, wAct);

    // Header row (textDim, no ugly TableHeadersRow chrome).
    ImGui::TableNextRow();
    ImGui::PushStyleColor(ImGuiCol_Text,
        ImVec4(palette.textDim[0], palette.textDim[1], palette.textDim[2], 1.0f));
    const char* heads[5] = {"Name", "Vendor", "Part # / CAD file", "Material / License", "Price"};
    for (int c = 0; c < 5; ++c) {
        ImGui::TableSetColumnIndex(c);
        cellText(heads[c]);
    }
    ImGui::PopStyleColor();

    const float rowH   = ImGui::GetFrameHeight();
    const float iconSz = 18.0f * sc;
    ImDrawList* dl = ImGui::GetWindowDrawList();

    // Body rows are fixed-height. Include the table's vertical cell padding in the clipper
    // stride: TableNextRow's requested height covers the Selectable, while the table adds
    // CellPadding above and below it.
    const float rowStride = rowH + 2.0f * ImGui::GetStyle().CellPadding.y;
    ImGuiListClipper clipper;
    clipper.Begin(static_cast<int>(S().results.size()), rowStride);
    while (clipper.Step()) {
        const int visibleCount = std::max(1, clipper.DisplayEnd - clipper.DisplayStart);
        requestThumbWindow(st, clipper.DisplayStart, clipper.DisplayEnd, visibleCount);

        for (int itemIndex = clipper.DisplayStart; itemIndex < clipper.DisplayEnd; ++itemIndex) {
        auto& item = S().results[itemIndex];
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
        // The overflow popup floats OUTSIDE the row, so plain hover would drop the action
        // cell (and with it the BeginPopup call) the instant the pointer left the row to
        // reach the popup.
        bool hovered = ImGui::IsItemHovered(
                           ImGuiHoveredFlags_AllowWhenOverlappedByItem |
                           ImGuiHoveredFlags_AllowWhenBlockedByActiveItem) ||
                       ImGui::IsPopupOpen("##rowmore");
        ImVec2 rMin = ImGui::GetItemRectMin();
        ImVec2 rMax = ImGui::GetItemRectMax();
        // Domain colour: the SAME hue the grid card and the category tree use, so a
        // fastener reads as a fastener in every view. Painted from the row-spanning
        // Selectable's rect (SpanAllColumns → the item rect covers every column) into the
        // table's BACKGROUND channel: the cell-0 channel we are in is clipped to the Name
        // column, which is what cut the wash off at the Name/Part # seam. The background
        // channel sits under every column's content, so the wash never covers text either.
        const float* tint = categoryTint(item.domain);
        ImGui::TablePushBackgroundChannel();
        ui::coloredRowWash(dl, rMin, rMax, 5.0f, tint, hovered ? 0.26f : 0.11f,
                           /*selected=*/false);
        if (hovered) {
            // Hover reads as the brighter wash above + the border treatment; the old flat
            // treeHover fill is gone — a grey slab over the hue undid the colour coding.
            dl->AddRect(ImVec2(rMin.x + 0.5f, rMin.y + 0.5f),
                        ImVec2(rMax.x - 0.5f, rMax.y - 0.5f),
                        Palette::toU32(palette.accentMid), 4.0f * sc, 0, 1.0f * sc);
        }
        ImGui::TablePopBackgroundChannel();
        itemTooltipAt(item, hovered && ImGui::IsItemHovered(ImGuiHoveredFlags_DelayNormal |
                                                            ImGuiHoveredFlags_AllowWhenOverlappedByItem));

        // Every cell's content sits on the row pill's vertical centre line. A bare
        // TableSetColumnIndex drops it at the row's TOP, where the text touches the pill's
        // border; the name cell already centred itself by hand, so the rest now match it.
        // The x is untouched — CellPadding already insets it by partsPadX() from the row.
        auto cellLine = [&](int col) {
            ImGui::TableSetColumnIndex(col);
            const ImVec2 c = ImGui::GetCursorScreenPos();
            ImGui::SetCursorScreenPos(
                ImVec2(c.x, rowPos.y + (rowH - ImGui::GetTextLineHeight()) * 0.5f));
        };

        // ---- Name cell: thumbnail (or domain icon fallback) + status badges + name ----
        ImGui::SetCursorScreenPos(rowPos);
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
            // The wash alone only says "empty, breathing"; the mark turning on top of it is
            // what says a fetch is running right now. 0.78 of the 18dp slot clears the mark's
            // legibility floor at 1x while still leaving a margin inside the wash rect — at
            // 0.55 it fell under the floor and the branch never fired at default DPI.
            const float markSz = iconSz * 0.78f;
            if (markSz >= ui::kTesseractSpinnerMinPx)
                ui::drawTesseractSpinner(
                    dl, ImVec2(rowPos.x + iconSz * 0.5f, rowPos.y + rowH * 0.5f), markSz,
                    ImGui::GetTime(),
                    ImGui::ColorConvertFloat4ToU32(ImVec4(tint[0], tint[1], tint[2], 1.0f)));
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
        // local one, carrying the vendor's favicon when one is already cached. Capped at
        // 96dp AND at a share of what is left of the cell, so a long vendor can never shove
        // the part name out of a narrow Name column; below one readable glyph it is dropped
        // (the Vendor column / row tooltip still name the source).
        {
            const unsigned int logoTex = vendorLogoTex(item);
            const bool  hasLogo = logoTex != 0;
            const float capW    = std::min(96.0f * sc + badgeLogoAdvance(hasLogo),
                                           ImGui::GetContentRegionAvail().x * 0.45f);
            if (capW >= badgeW("W\xe2\x80\xa6", hasLogo)) {
                badgeInline(fitBadgeLabel(sourceBadgeLabel(item), capW, hasLogo).c_str(),
                            sourceBadgeRole(item), logoTex);
                ImGui::SameLine();
            }
        }
        cellText(item.name.c_str());

        auto dimCell = [&](int col, const char* text) {
            cellLine(col);
            ImGui::PushStyleColor(ImGuiCol_Text,
                ImVec4(palette.textDim[0], palette.textDim[1], palette.textDim[2], 1.0f));
            cellText(text);
            ImGui::PopStyleColor();
        };
        dimCell(1, item.vendor.c_str());
        dimCell(2, item.partNumber.c_str());
        dimCell(3, isCommunityCad(item) && !item.license.empty()
                   ? item.license.c_str() : (item.material.empty() ? "-" : item.material.c_str()));

        cellLine(4);
        {
            // The one number a buyer scans a results table for — it should not be the same
            // tone as the part number three columns to its left.
            const float* role = item.priceUsd > 0.0 ? palette.accent : palette.textDisabled;
            char price[64];
            priceLabel(item, price, sizeof(price));
            ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(role[0], role[1], role[2], 1.0f));
            cellText(price);
            ImGui::PopStyleColor();
        }

        cellLine(5);
        if (hovered) {
            ImGui::PushStyleColor(ImGuiCol_Button,
                ImVec4(palette.button[0], palette.button[1],
                       palette.button[2], palette.button[3]));
            actionsLayout(st, item);
            ImGui::PopStyleColor();
        }

        ImGui::PopID();
    }
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

    // Column packing: n cards fit when n*(cardW+gap) <= availW+gap, where availW already has
    // the scroll child's pill-scrollbar lane taken out of it — otherwise the right-hand card
    // of every row runs under the bar. Whatever is left over past the last column stays a
    // TRAILING MARGIN: the grid is LEFT aligned on the pane's content origin (the same line
    // the "Parts (N)" section header starts at) rather than centred, which used to push the
    // first card off that line by up to half a column. A card is never wider than the pane.
    const float laneW    = ui::metrics::px(ui::kChatScrollbarLane);
    const float availW   = std::max(ImGui::GetContentRegionAvail().x - laneW,
                                    ui::metrics::U(12));
    const float cardW    = std::min(168.0f * sc, availW);
    const int   cols     = std::max(1, (int)((availW + gap) / (cardW + gap)));
    // The card reserves `pad` on BOTH sides; the floor keeps a squeezed pane from producing a
    // negative content width (which would make the ellipsize loop and AddImage draw garbage).
    const float contentW = std::max(ui::metrics::U(4), cardW - 2.0f * pad);
    // lineH / titleH / bandH computed above — the card height depends on them.
    ImDrawList* dl       = ImGui::GetWindowDrawList();
    const float gridX   = ImGui::GetCursorScreenPos().x;

    ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing, ImVec2(gap, gap));  // uniform row gap
    const int itemCount = static_cast<int>(S().results.size());
    const int rowCount  = (itemCount + cols - 1) / cols;
    ImGuiListClipper clipper;
    clipper.Begin(rowCount, cardH + gap);
    while (clipper.Step()) {
        const int visibleRows = std::max(1, clipper.DisplayEnd - clipper.DisplayStart);
        requestThumbWindow(st, clipper.DisplayStart * cols,
                           std::min(itemCount, clipper.DisplayEnd * cols),
                           visibleRows * cols);

        for (int row = clipper.DisplayStart; row < clipper.DisplayEnd; ++row) {
            const ImVec2 rowOrigin(gridX, ImGui::GetCursorScreenPos().y);
            for (int col = 0; col < cols; ++col) {
                const int itemIndex = row * cols + col;
                if (itemIndex >= itemCount)
                    break;
                ImGui::SetCursorScreenPos(
                    ImVec2(rowOrigin.x + col * (cardW + gap), rowOrigin.y));
                auto& item = S().results[itemIndex];
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
        // their VENDOR in amber — with its favicon when the logo is already cached — and
        // local hits read "library" in accent.
        {
            const unsigned int logoTex = vendorLogoTex(item);
            const bool  hasLogo = logoTex != 0;
            // The logo's advance is ADDED to the 72% cap so the icon does not eat the label,
            // then the whole pill is held inside the card's content line — on a squeezed pane
            // 72% + 18dp would otherwise reach past cx and be clipped mid-glyph.
            const float capW    = std::min(contentW * 0.72f + badgeLogoAdvance(hasLogo),
                                           contentW);
            const std::string sb = fitBadgeLabel(sourceBadgeLabel(item), capW, hasLogo);
            drawBadge(dl, ImVec2(p1.x - pad - badgeW(sb.c_str(), hasLogo), markY), sb.c_str(),
                      sourceBadgeRole(item), logoTex);
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
        // Community CAD licenses matter more than an absent commerce price.
        char pbuf[48];
        if (isCommunityCad(item) && !item.license.empty()) {
            contentText(ty, item.license.c_str(), Palette::toU32(palette.textDim));
        } else {
            if (item.priceUsd > 0.0) std::snprintf(pbuf, sizeof(pbuf), "$%.2f", item.priceUsd);
            else                     std::snprintf(pbuf, sizeof(pbuf), "%s", "—");
            contentText(ty, pbuf, item.priceUsd > 0.0 ? Palette::toU32(palette.accent)
                                                      : Palette::toU32(palette.textDisabled));
        }

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
                                             contentW - (n > 1 ? lineH + gapX : 0.0f));

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
            // next column or clipped-row bookkeeping.
            ImGui::SetCursorScreenPos(p0);
            ImGui::Dummy(ImVec2(cardW, cardH));
        }

                ImGui::PopID();
            }
        }
    }
    ImGui::PopStyleVar();  // ItemSpacing
}

}  // namespace parts
}  // namespace ui
