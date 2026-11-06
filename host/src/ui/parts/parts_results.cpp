// Parts Library result rows, cards, and selected-part details.
#include <algorithm>
#include <cfloat>
#include <cstdint>
#include <cstdio>
#include <string>

#include "imgui.h"
#include "core/app_state.h"
#include "ui_theme.h"
#include "ui/file_dialog.h"
#include "ui/metrics.h"
#include "ui/web_capabilities.h"
#include "ui/widgets/kit.h"
#include "parts_internal.h"
#include "parts_widgets.h"

namespace ui {
namespace parts {
namespace {

constexpr const char* kMissing = "\xe2\x80\x94";

enum class Action { Download, Insert, Reverse, Generate };

const PartsItem* selectedItem() {
    for (const auto& item : S().results)
        if (item.id == S().selectedId) return &item;
    return nullptr;
}

const char* disabledReason(const PartsItem& item, Action action) {
    const auto op = S().itemOperations.find(item.id);
    if (op != S().itemOperations.end() && op->second.busy)
        return "Another action for this part is in progress.";
    if (S().insertBusy && S().insertItemId == item.id)
        return "Insertion of this part is in progress.";
    switch (action) {
    case Action::Download:
        if (item.downloaded) return "This model is already downloaded.";
        if (!item.downloadable && item.id.rfind("catalog:", 0) != 0)
            return "No download is available for this part.";
        break;
    case Action::Insert:
        if (S().insertBusy) return "Another insertion is in progress.";
        if (!item.placeable) return "This part has no model available to insert.";
        break;
    case Action::Reverse:
        if (item.source != "live") return "Reverse engineering is available for web results.";
        break;
    case Action::Generate:
        if (item.placeable || item.downloadable)
            return "A model is already available for this part.";
        break;
    }
    return nullptr;
}

void runAction(AppState& st, const PartsItem& item, Action action) {
    if (disabledReason(item, action)) return;
    switch (action) {
    case Action::Download: queueDownload(st, item); break;
    case Action::Insert: insertItem(st, item); break;
    case Action::Reverse: sendToReverseEngineering(st, item); break;
    case Action::Generate: generateModelFromLibrary(st, item); break;
    }
}

std::string priceLabel(const PartsItem& item) {
    if (item.price <= 0.0) return kMissing;
    char value[64];
    if (item.currency == "USD") std::snprintf(value, sizeof(value), "$%.2f", item.price);
    else if (item.currency.empty()) std::snprintf(value, sizeof(value), "%.2f (currency unknown)", item.price);
    else std::snprintf(value, sizeof(value), "%.2f %s", item.price, item.currency.c_str());
    return value;
}

std::string sourceLabel(const PartsItem& item) {
    if (item.source != "live") return "Library";
    return item.vendor.empty() ? "Web" : item.vendor;
}

std::string identityLine(const PartsItem& item) {
    std::string line;
    if (!item.family.empty() && item.name.find(item.family) == std::string::npos)
        line = item.family;
    const std::string& code = !item.designation.empty() ? item.designation : item.partNumber;
    if (!code.empty()) {
        if (!line.empty()) line += " \xc2\xb7 ";
        line += code;
    }
    return line.empty() ? kMissing : line;
}

std::string makerLine(const PartsItem& item) {
    std::string line = sourceLabel(item);
    const std::string& maker = !item.manufacturer.empty() ? item.manufacturer : item.vendor;
    if (!maker.empty() && maker != line) line += " \xc2\xb7 " + maker;
    return line;
}

void drawTextFit(ImDrawList* dl, ImVec2 pos, float width, const std::string& text, ImU32 color) {
    if (width <= 1.0f) return;
    if (ImGui::CalcTextSize(text.c_str()).x <= width) dl->AddText(pos, color, text.c_str());
    else {
        const std::string shown = ui::kit::elide(text.c_str(), width);
        dl->AddText(pos, color, shown.c_str());
    }
}

void drawCardTitle(ImDrawList* dl, ImVec2 pos, float width, const std::string& text) {
    const ImU32 color = Palette::toU32(palette.text);
    if (ImGui::CalcTextSize(text.c_str()).x <= width) {
        dl->AddText(pos, color, text.c_str());
        return;
    }
    ImFont* font = ImGui::GetFont();
    const float scale = ImGui::GetFontSize() / std::max(font->FontSize, 1.0f);
    const char* first = text.c_str();
    const char* end = first + text.size();
    const char* split = font->CalcWordWrapPositionA(scale, first, end, width);
    if (split <= first) {
        drawTextFit(dl, pos, width, text, color);
        return;
    }
    const char* lineEnd = split;
    while (lineEnd > first && (lineEnd[-1] == ' ' || lineEnd[-1] == '\t')) --lineEnd;
    dl->AddText(pos, color, first, lineEnd);
    while (split < end && (*split == ' ' || *split == '\t')) ++split;
    if (split == end) return;
    const ImVec2 next(pos.x, pos.y + ImGui::GetTextLineHeight());
    if (ImGui::CalcTextSize(split, end).x <= width)
        dl->AddText(next, color, split, end);
    else {
        const std::string shown = ui::kit::elide(split, width);
        dl->AddText(next, color, shown.c_str());
    }
}

void drawMakerLine(ImDrawList* dl, ImVec2 pos, float width, const PartsItem& item) {
    if (item.source == "live" && item.id.compare(0, 8, "catalog:") == 0) {
        const size_t sep = item.id.find("::", 8);
        if (sep != std::string::npos && sep > 8) {
            const std::string key = "vendorlogo:" + item.id.substr(8, sep - 8);
            const auto logo = S().thumbs.find(key);
            if (logo != S().thumbs.end() && logo->second.tex) {
                const float size = std::min(ImGui::GetTextLineHeight(), 14.0f * ui::uiScale);
                logo->second.lastUse = ImGui::GetTime();
                dl->AddImage((ImTextureID)(intptr_t)logo->second.tex, pos,
                             ImVec2(pos.x + size, pos.y + size));
                pos.x += size + 4.0f * ui::uiScale;
                width -= size + 4.0f * ui::uiScale;
            }
        }
    }
    drawTextFit(dl, pos, width, makerLine(item), Palette::toU32(palette.textDim));
}

// A missing preview is never represented by a domain glyph or a vendor logo.
void drawPreview(const PartsItem& item, ImVec2 p0, ImVec2 p1) {
    ImDrawList* dl = ImGui::GetWindowDrawList();
    float bg[4];
    Palette::withAlpha(palette.frameBg, bg, 0.65f);
    dl->AddRectFilled(p0, p1, Palette::toU32(bg), 5.0f * ui::uiScale);
    const auto it = S().thumbs.find(item.id);
    if (it != S().thumbs.end() && it->second.tex && it->second.w > 0 && it->second.h > 0) {
        it->second.lastUse = ImGui::GetTime();
        const float pad = 3.0f * ui::uiScale;
        const float w = std::max(1.0f, p1.x - p0.x - 2.0f * pad);
        const float h = std::max(1.0f, p1.y - p0.y - 2.0f * pad);
        const float fit = std::min(w / it->second.w, h / it->second.h);
        const float dw = it->second.w * fit, dh = it->second.h * fit;
        const ImVec2 a((p0.x + p1.x - dw) * 0.5f, (p0.y + p1.y - dh) * 0.5f);
        dl->AddImage((ImTextureID)(intptr_t)it->second.tex, a, ImVec2(a.x + dw, a.y + dh));
        return;
    }
    const bool failed = S().thumbFailed.count(item.id) != 0;
    const auto reason = S().thumbErrors.find(item.id);
    const bool missing = reason == S().thumbErrors.end() || reason->second == "no_preview";
    const char* label = !failed ? "Loading\xe2\x80\xa6" : (missing ? "No preview" : "Preview error");
    ImFont* font = ImGui::GetFont();
    float fs = ImGui::GetFontSize() * 0.75f;
    ImVec2 sz = font->CalcTextSizeA(fs, FLT_MAX, 0.0f, label);
    const float available = std::max(1.0f, p1.x - p0.x - 4.0f * ui::uiScale);
    dl->PushClipRect(p0, p1, true);
    if (failed && sz.x > available) {
        const char* lines[2] = {missing ? "No" : "Preview", missing ? "preview" : "error"};
        const float y = (p0.y + p1.y - fs * 2.0f) * 0.5f;
        for (int i = 0; i < 2; ++i) {
            const ImVec2 line = font->CalcTextSizeA(fs, FLT_MAX, 0.0f, lines[i]);
            dl->AddText(font, fs, ImVec2((p0.x + p1.x - line.x) * 0.5f, y + fs * i),
                        Palette::toU32(palette.textDim), lines[i]);
        }
    } else {
        if (sz.x > available) {
            fs *= available / sz.x;
            sz = font->CalcTextSizeA(fs, FLT_MAX, 0.0f, label);
        }
        dl->AddText(font, fs, ImVec2((p0.x + p1.x - sz.x) * 0.5f,
                                      (p0.y + p1.y - sz.y) * 0.5f),
                    Palette::toU32(palette.textDim), label);
    }
    dl->PopClipRect();
}

void requestVisible(AppState& st, int first, int end, int margin) {
    const int count = static_cast<int>(S().results.size());
    first = std::clamp(first, 0, count);
    end = std::clamp(end, first, count);
    bool visibleReady = !S().liveStarting;
    for (int i = first; i < end; ++i) {
        const std::string& id = S().results[i].id;
        S().visibleThumbs.insert(id);
        if (!S().liveStarting) requestThumb(st, S().results[i]);
        if (!S().thumbs.count(id) && !S().thumbFailed.count(id)) visibleReady = false;
    }
    for (int i = end; i-- > first;) prioritizeThumb(S().results[i].id);
    // Native renders share a serialized engine. Do not let speculative offscreen
    // previews occupy a batch until every visible result has an answer.
    if (!visibleReady) return;
    for (int i = std::max(0, first - margin); i < first; ++i) requestThumb(st, S().results[i]);
    for (int i = end; i < std::min(count, end + margin); ++i) requestThumb(st, S().results[i]);
}

bool resultsPreamble(AppState& st) {
    if (!S().results.empty()) return false;
    if (S().searching || S().liveStarting || S().liveSearching) {
        spinner("Searching parts");
        return true;
    }
    const bool sourceFailure = std::any_of(S().notices.begin(), S().notices.end(),
        [](const Notice& n) { return n.reason != "native_browse_deferred"; });
    if (!S().searchErr.empty()) {
        ImGui::TextWrapped("Parts search unavailable: %s", S().searchErr.c_str());
    } else if (!S().searchDone) {
        emptyState("parts_all", "Search for parts", "Enter a search or select a category.");
        return true;
    } else if (sourceFailure && (S().sourceFilter == 2 || S().localTotal == 0)) {
        emptyState("cancel_x", "Sources unavailable", "One or more sources could not be searched.");
    } else if (S().searchBuf[0] || !S().selectedCategory.empty() || activeFilterCount()) {
        emptyState("cancel_x", "No matching parts", "Try another search or clear the filters.");
    } else if (!S().totalExact) {
        emptyState("parts_all", "No indexed parts", "Search or choose a category to explore other sources.");
    } else {
        emptyState("parts_all", "The catalog is empty", "Try a web search or check your sources.");
    }
    if (ImGui::Button("Retry")) doSearch(st);
    if (activeFilterCount() > 0) {
        ImGui::SameLine();
        if (ImGui::Button("Clear filters")) clearPartFilters();
    }
    return true;
}

void scrollToSelection(float stride, int columns) {
    if (!S().scrollToSelected) return;
    for (int i = 0; i < static_cast<int>(S().results.size()); ++i) {
        if (S().results[i].id == S().selectedId) {
            const float top = ImGui::GetCursorPosY() + (i / columns) * stride;
            const float scroll = ImGui::GetScrollY();
            const float visible = std::max(1.0f, ImGui::GetWindowHeight() -
                                                   2.0f * ImGui::GetStyle().WindowPadding.y);
            if (top < scroll) ImGui::SetScrollY(top);
            else if (top + stride > scroll + visible)
                ImGui::SetScrollY(std::max(0.0f, top + stride - visible));
            break;
        }
    }
    S().scrollToSelected = false;
}

void rowTooltip(const PartsItem& item) {
    if (!ImGui::IsItemHovered(ImGuiHoveredFlags_DelayNormal)) return;
    ImGui::BeginTooltip();
    ImGui::TextUnformatted(item.name.c_str());
    ImGui::TextDisabled("%s", makerLine(item).c_str());
    ImGui::TextDisabled("%s", identityLine(item).c_str());
    ImGui::EndTooltip();
}

void detailField(const char* label, const std::string& value) {
    ImGui::TextDisabled("%s", label);
    ImGui::TextWrapped("%s", value.empty() ? kMissing : value.c_str());
}

void detailAction(AppState& st, const PartsItem& item, const char* label, Action action) {
    const char* reason = disabledReason(item, action);
    ImGui::BeginDisabled(reason != nullptr);
    const bool clicked = ImGui::Button(label, ImVec2(-1.0f, 0.0f));
    ImGui::EndDisabled();
    if (reason && ImGui::IsItemHovered(ImGuiHoveredFlags_AllowWhenDisabled)) ImGui::SetTooltip("%s", reason);
    if (clicked && !reason) runAction(st, item, action);
}

} // namespace

void handlePartsSelectionKeys(AppState& st) {
    if (!ImGui::IsWindowFocused(ImGuiFocusedFlags_ChildWindows) || ImGui::GetIO().WantTextInput ||
        ImGui::IsAnyItemActive() || ImGui::IsPopupOpen("", ImGuiPopupFlags_AnyPopupId))
        return;
    if (ImGui::IsKeyPressed(ImGuiKey_Escape) && !S().selectedId.empty()) {
        S().selectedId.clear();
        return;
    }
    const int count = static_cast<int>(S().results.size());
    if (!count) return;
    int current = -1;
    for (int i = 0; i < count; ++i)
        if (S().results[i].id == S().selectedId) { current = i; break; }
    const bool down = ImGui::IsKeyPressed(ImGuiKey_DownArrow);
    const bool up = ImGui::IsKeyPressed(ImGuiKey_UpArrow);
    if (up || down) {
        const int next = current < 0 ? (down ? 0 : count - 1) :
            std::clamp(current + (down ? 1 : -1), 0, count - 1);
        S().selectedId = S().results[next].id;
        S().scrollToSelected = true;
    }
    if (ImGui::IsKeyPressed(ImGuiKey_Enter)) {
        if (const PartsItem* item = selectedItem()) {
            for (Action action : {Action::Insert, Action::Download, Action::Reverse, Action::Generate}) {
                if (!disabledReason(*item, action)) { runAction(st, *item, action); break; }
            }
        }
    }
}

void drawResultsList(AppState& st) {
    if (resultsPreamble(st)) return;
    const float sc = ui::uiScale;
    const float gap = 6.0f * sc;
    const float preview = ui::metrics::partsRowPreview();
    const float rowH = std::max(preview + 12.0f * sc, 3.0f * ImGui::GetTextLineHeight() + 12.0f * sc);
    const float width = std::max(1.0f, ImGui::GetContentRegionAvail().x - ui::metrics::px(ui::kChatScrollbarLane));
    const float stride = rowH + gap;
    const float lineH = ImGui::GetTextLineHeight();
    ImDrawList* dl = ImGui::GetWindowDrawList();
    scrollToSelection(stride, 1);
    ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing, ImVec2(0.0f, gap));
    ImGuiListClipper clipper;
    clipper.Begin(static_cast<int>(S().results.size()), stride);
    while (clipper.Step()) {
        requestVisible(st, clipper.DisplayStart, clipper.DisplayEnd,
                       std::max(1, clipper.DisplayEnd - clipper.DisplayStart));
        for (int i = clipper.DisplayStart; i < clipper.DisplayEnd; ++i) {
            const PartsItem& item = S().results[i];
            ImGui::PushID(item.id.c_str());
            const ImVec2 p0 = ImGui::GetCursorScreenPos(), p1(p0.x + width, p0.y + rowH);
            if (ImGui::InvisibleButton("##row", ImVec2(width, rowH))) S().selectedId = item.id;
            const bool hovered = ImGui::IsItemHovered();
            rowTooltip(item);
            const bool selected = S().selectedId == item.id;
            ui::coloredRowWash(dl, p0, p1, 5.0f, categoryTint(item.domain), hovered ? 0.24f : 0.10f, selected);
            borderCardRect(dl, p0, p1, selected, hovered, 5.0f);
            const ImVec2 img0(p0.x + 6.0f * sc, p0.y + (rowH - preview) * 0.5f);
            drawPreview(item, img0, ImVec2(img0.x + preview, img0.y + preview));
            const float textX = img0.x + preview + 9.0f * sc;
            const float textW = std::max(1.0f, p1.x - textX - 8.0f * sc);
            const float top = p0.y + (rowH - 3.0f * lineH) * 0.5f;
            dl->PushClipRect(ImVec2(textX, p0.y), p1, true);
            drawTextFit(dl, ImVec2(textX, top), textW, item.name, Palette::toU32(palette.text));
            drawMakerLine(dl, ImVec2(textX, top + lineH), textW, item);
            const std::string price = priceLabel(item);
            const float priceW = std::min(textW * 0.55f, ImGui::CalcTextSize(price.c_str()).x);
            drawTextFit(dl, ImVec2(textX, top + 2.0f * lineH), std::max(1.0f, textW - priceW - 8.0f * sc),
                        identityLine(item), Palette::toU32(palette.textDim));
            drawTextFit(dl, ImVec2(textX + textW - priceW, top + 2.0f * lineH), priceW, price,
                        Palette::toU32(item.price > 0 ? palette.accent : palette.textDisabled));
            dl->PopClipRect();
            ImGui::PopID();
        }
    }
    ImGui::PopStyleVar();
}

void drawResultsGrid(AppState& st) {
    if (resultsPreamble(st)) return;
    const float sc = ui::uiScale, gap = 8.0f * sc, pad = ui::metrics::partsPadX();
    const float width = std::max(1.0f, ImGui::GetContentRegionAvail().x - ui::metrics::px(ui::kChatScrollbarLane));
    const float cardW = std::min(190.0f * sc, width);
    const int cols = std::max(1, static_cast<int>((width + gap) / (cardW + gap)));
    const float imgH = ui::metrics::partsGridPreview();
    const float lineH = ImGui::GetTextLineHeight();
    const float cardH = imgH + 5.0f * lineH + 16.0f * sc;
    const float stride = cardH + gap;
    const int count = static_cast<int>(S().results.size());
    const int rows = (count + cols - 1) / cols;
    const float startX = ImGui::GetCursorScreenPos().x;
    ImDrawList* dl = ImGui::GetWindowDrawList();
    scrollToSelection(stride, cols);
    ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing, ImVec2(gap, gap));
    ImGuiListClipper clipper;
    clipper.Begin(rows, stride);
    while (clipper.Step()) {
        requestVisible(st, clipper.DisplayStart * cols, std::min(count, clipper.DisplayEnd * cols),
                       std::max(cols, (clipper.DisplayEnd - clipper.DisplayStart) * cols));
        for (int row = clipper.DisplayStart; row < clipper.DisplayEnd; ++row) {
            const float y = ImGui::GetCursorScreenPos().y;
            for (int col = 0; col < cols; ++col) {
                const int i = row * cols + col;
                if (i >= count) break;
                const PartsItem& item = S().results[i];
                ImGui::PushID(item.id.c_str());
                ImGui::SetCursorScreenPos(ImVec2(startX + col * (cardW + gap), y));
                const ImVec2 p0 = ImGui::GetCursorScreenPos(), p1(p0.x + cardW, p0.y + cardH);
                if (ImGui::InvisibleButton("##card", ImVec2(cardW, cardH))) S().selectedId = item.id;
                const bool hovered = ImGui::IsItemHovered();
                rowTooltip(item);
                const bool selected = S().selectedId == item.id;
                ui::glassRowBackground(dl, p0, p1, 8.0f, false, false, 0.20f);
                ui::glassCardWash(dl, p0, p1, 8.0f, categoryTint(item.domain), hovered ? 0.22f : 0.12f);
                borderCardRect(dl, p0, p1, selected, hovered, 8.0f);
                drawPreview(item, ImVec2(p0.x + pad, p0.y + 6.0f * sc),
                            ImVec2(p1.x - pad, p0.y + imgH));
                const float x = p0.x + pad, w = std::max(1.0f, cardW - 2.0f * pad);
                float ty = p0.y + imgH + 4.0f * sc;
                dl->PushClipRect(p0, p1, true);
                drawCardTitle(dl, ImVec2(x, ty), w, item.name); ty += 2.0f * lineH;
                drawMakerLine(dl, ImVec2(x, ty), w, item); ty += lineH;
                drawTextFit(dl, ImVec2(x, ty), w, identityLine(item), Palette::toU32(palette.textDim)); ty += lineH;
                drawTextFit(dl, ImVec2(x, ty), w, priceLabel(item),
                            Palette::toU32(item.price > 0 ? palette.accent : palette.textDisabled));
                dl->PopClipRect();
                ImGui::PopID();
            }
        }
    }
    ImGui::PopStyleVar();
}

void drawPartDetails(AppState& st) {
    const PartsItem* found = selectedItem();
    if (!found) {
        if (!S().searching && !S().liveStarting && !S().liveSearching) S().selectedId.clear();
        return;
    }
    handlePartsSelectionKeys(st);
    if (S().selectedId.empty()) return;
    // Keyboard navigation may have selected another row.
    found = selectedItem();
    if (!found) return;
    const PartsItem& part = *found;
    ImGui::PushID(part.id.c_str());
    ImGui::TextUnformatted("Part details");
    const float closeW = ImGui::CalcTextSize("Close").x + 2.0f * ImGui::GetStyle().FramePadding.x;
    ImGui::SameLine(std::max(ImGui::GetCursorPosX(), ImGui::GetWindowContentRegionMax().x - closeW));
    if (ImGui::Button("Close")) { S().selectedId.clear(); ImGui::PopID(); return; }
    S().visibleThumbs.insert(part.id);
    if (!S().liveStarting) {
        requestThumb(st, part);
        prioritizeThumb(part.id);
    }
    const float imageW = std::max(1.0f, ImGui::GetContentRegionAvail().x - ui::metrics::px(ui::kChatScrollbarLane));
    const float imageH = std::min(190.0f * ui::uiScale, imageW * 0.8f);
    const ImVec2 a = ImGui::GetCursorScreenPos();
    ImGui::Dummy(ImVec2(imageW, imageH));
    drawPreview(part, a, ImVec2(a.x + imageW, a.y + imageH));
    const auto thumb = S().thumbs.find(part.id);
    if (thumb != S().thumbs.end()) {
        const char* provenance = thumb->second.provenance == "cad_render" ? "CAD render" :
            (thumb->second.provenance == "product_image" ? "Product photo" : "Preview (origin unknown)");
        ImGui::TextDisabled("%s", provenance);
    } else if (S().thumbFailed.count(part.id)) {
        auto reason = S().thumbErrors.find(part.id);
        if (reason != S().thumbErrors.end() && reason->second != "no_preview") {
            ImGui::TextDisabled("Preview unavailable: %s", reason->second.c_str());
            if (ImGui::SmallButton("Retry preview")) retryThumb(st, part);
        }
    }
    ImGui::TextWrapped("%s", part.name.c_str());
    if (part.source == "live" && !part.placeable && part.kind != "model") {
        ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(palette.textDim[0], palette.textDim[1],
                                                    palette.textDim[2], 1.0f));
        ImGui::TextWrapped("The source has not confirmed an insertable CAD model.");
        ImGui::PopStyleColor();
    }
    auto op = S().itemOperations.find(part.id);
    if (op != S().itemOperations.end()) {
        if (!op->second.status.empty()) ImGui::TextWrapped("%s", op->second.status.c_str());
        if (!op->second.error.empty()) ImGui::TextWrapped("%s", op->second.error.c_str());
        if (!S().queueError.empty() && !op->second.jobId.empty())
            ImGui::TextWrapped("Queue status unavailable: %s", S().queueError.c_str());
    }
    if (S().insertItemId == part.id && !S().insertNote.empty())
        ImGui::TextWrapped("%s", S().insertNote.c_str());
    if ((op != S().itemOperations.end() && op->second.busy) ||
        (S().insertBusy && S().insertItemId == part.id))
        ui::kit::progressBar(-1.0f, nullptr, ui::kit::Tone::Accent, 3.0f * ui::uiScale);
    if (op != S().itemOperations.end() && !op->second.jobId.empty()) {
        for (const auto& job : S().queue) {
            if (job.id != op->second.jobId || job.candidates.empty()) continue;
            if (!job.askUser.empty()) ImGui::TextWrapped("%s", job.askUser.c_str());
            ImGui::BeginDisabled(job.importing);
            if (ImGui::BeginCombo("##model_choice", job.importing ? "Selecting…" : "Choose a model")) {
                if (ImGui::Selectable("Do not download")) dismissQueuedChoice(job.id);
                for (const auto& member : job.candidates) {
                    const auto label = job.candidateLabels.find(member);
                    const std::string& text = label == job.candidateLabels.end() ? member : label->second;
                    ImGui::PushID(member.c_str());
                    ImGui::BeginDisabled(!job.canSelect);
                    if (ImGui::Selectable(text.c_str())) selectQueuedArtifact(st, job.id, member);
                    ImGui::EndDisabled();
                    if (!job.canSelect && ImGui::IsItemHovered(ImGuiHoveredFlags_AllowWhenDisabled))
                        ImGui::SetTooltip("The original search result is unavailable.");
                    ImGui::PopID();
                }
                ImGui::EndCombo();
            }
            ImGui::EndDisabled();
            break;
        }
    }
    detailAction(st, part, "Download", Action::Download);
    detailAction(st, part, "Insert", Action::Insert);
    detailAction(st, part, "Reverse-engineer", Action::Reverse);
    detailAction(st, part, "Generate 3D model", Action::Generate);
    if (op != S().itemOperations.end() && !op->second.jobId.empty()) {
        for (const auto& job : S().queue) {
            if (job.id != op->second.jobId || job.status != "done" ||
                job.artifactKind != "cad" || !job.cadValidated || job.artifactPath.empty()) continue;
            ImGui::BeginDisabled(job.importing);
            if (ImGui::Button(job.importing ? "Importing…" : "Import downloaded CAD", ImVec2(-1, 0)))
                importQueuedArtifact(st, job.id, job.artifactPath);
            ImGui::EndDisabled();
            break;
        }
    }
    ImGui::Separator();
    detailField("Part ID", part.id);
    detailField("Source", sourceLabel(part));
    detailField("Manufacturer / vendor", !part.manufacturer.empty() ? part.manufacturer : part.vendor);
    detailField("Family", part.family);
    detailField("Designation", part.designation);
    detailField("MPN", part.mpn);
    detailField("Part number / SKU", part.partNumber);
    detailField("Material", part.material);
    detailField("Price", priceLabel(part));
    detailField("Stock", part.stock >= 0 ? std::to_string(part.stock) : std::string());
    std::string formats;
    for (const auto& format : part.modelFormats) {
        if (!formats.empty()) formats += ", ";
        formats += format;
    }
    detailField("Model formats", formats);
    if (!part.kind.empty()) detailField("Result type", part.kind);
    if (!part.fileName.empty()) detailField("CAD file", part.fileName);
    if (!part.standard.empty()) detailField("Standard", part.standard);
    if (!part.license.empty()) detailField("License", part.license);
    if (!part.description.empty()) detailField("Details", part.description);
    if (!part.sizes.empty()) {
        std::string sizes;
        for (const auto& size : part.sizes) { if (!sizes.empty()) sizes += ", "; sizes += size; }
        detailField("Available sizes", sizes);
    }
    const auto urlAt = part.id.find("::http");
    if (urlAt != std::string::npos && ImGui::Button("Open product page")) {
        if (!ui::openExternalHttpUrl(part.id.substr(urlAt + 2)))
            st.toast(ui::ToastLevel::Warn, "Could not open product page");
    }
    if (!part.datasheet.empty() && ImGui::Button("Open datasheet")) {
        const bool web = part.datasheet.rfind("http://", 0) == 0 || part.datasheet.rfind("https://", 0) == 0;
        if (!(web ? ui::openExternalHttpUrl(part.datasheet) : ui::openExternalFile(part.datasheet)))
            st.toast(ui::ToastLevel::Warn, "Could not open datasheet");
    }
    ImGui::PopID();
}

} // namespace parts
} // namespace ui
