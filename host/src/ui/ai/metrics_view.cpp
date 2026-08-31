// See metrics_view.h.
#include "metrics_view.h"

#include <algorithm>
#include <cstdio>
#include <string>
#include <unordered_map>
#include <vector>

#include "imgui.h"
#include "imgui_internal.h"   // ImRect (glassPanel/adaptivePanel rects)

#include "core/app_state.h"
#include "ui_theme.h"
#include "ui/metrics.h"           // cpMenu* rhythm tokens / px / stroke
#include "ui/copilot/copilot.h"   // BridgeModelMeta / bridgeModelMeta

namespace ui {
namespace {

std::string fmtUsd(double v) {
    char buf[32];
    std::snprintf(buf, sizeof(buf), "$%.4g", v);
    return buf;
}

std::string fmtTok(long long t) {
    char buf[24];
    if (t >= 1000000)
        std::snprintf(buf, sizeof(buf), "%.1fM", t / 1.0e6);
    else if (t >= 1000)
        std::snprintf(buf, sizeof(buf), "%.1fk", t / 1.0e3);
    else
        std::snprintf(buf, sizeof(buf), "%lld", t);
    return buf;
}

// Same visual grammar as the other DAG-pane views: bold label + a thin accent underline.
void secHeader(const char* label) {
    ImGui::TextUnformatted(label);
    ImVec2 p0 = ImGui::GetItemRectMin();
    ImVec2 p1 = ImGui::GetItemRectMax();
    const float rule = ui::metrics::stroke(1.0f);
    ImGui::GetWindowDrawList()->AddLine(
        ImVec2(p0.x, p1.y + rule), ImVec2(p1.x, p1.y + rule),
        ImGui::ColorConvertFloat4ToU32(
            ImVec4(palette.accent[0], palette.accent[1], palette.accent[2], 0.60f)),
        rule);
    ImGui::Dummy(ImVec2(0, ui::metrics::cpMenuHeadGap()));
}

// Arc tone for a "share of a budget" ring — the SAME ladder the Copilot composer's
// context-window meter uses (ui::kUsageWarnFrac / kUsageCritFrac, the chud-ui-shared-v1
// usage thresholds) so one colour means one thing.
ImU32 fracArcCol(float frac) {
    return frac < kUsageWarnFrac ? Palette::toU32(palette.statusOk)
         : frac < kUsageCritFrac ? Palette::toU32(palette.statusWarn)
                                 : Palette::toU32(palette.statusErr);
}

// Compact "how full is this" badge: the shared circular gauge (ui::drawFractionRing) plus the
// value beside it. Replaces the per-view rectangular mini-bars — every compact fraction
// readout in the app is now the same circle.
void fractionBadge(const AdaptiveSurface& surf, float frac, const char* text) {
    ImDrawList* dl = ImGui::GetWindowDrawList();
    const float  r  = ImGui::GetFrameHeight() * 0.34f;
    const ImVec2 p  = ImGui::GetCursorScreenPos();
    const float  lh = ImGui::GetTextLineHeight();
    ImVec4 track = surf.textTertiary;
    track.w = 0.35f;
    drawFractionRing(dl, ImVec2(p.x + r, p.y + lh * 0.5f), r,
                     ui::metrics::stroke(2.0f), frac,
                     ImGui::ColorConvertFloat4ToU32(track), fracArcCol(frac));
    ImGui::Dummy(ImVec2(r * 2.0f, lh));
    ImGui::SameLine(0, ui::metrics::cpMenuLabelGap());
    ImGui::TextUnformatted(text);
}

struct ModelSpend {
    std::string provider, model;
    double usd = 0;
    long long tin = 0, tout = 0;
    // `priced` means the catalog carries a real price, so `usd` is computed. `localFree`
    // means inference runs locally and $0 is real. Otherwise the price is unknown.
    bool priced = false;
    bool localFree = false;
};

struct SessionSpend {
    std::string title;
    double usd = 0;
    long long tin = 0, tout = 0;
    int turns = 0;
    int unpricedTurns = 0;   // turns whose model has no catalog price -> usd is a floor
    bool active = false;
};

}  // namespace

void drawMetricsView(AppState& st) {
    const float S = ui::uiScale;

    // Same adaptive surface the host pane (drawViewPane) resolves — idempotent per frame, and
    // it gives the fraction rings a track tone that matches the glass they sit on.
    const ImVec2 paneMin = ImGui::GetWindowPos();
    const ImRect paneRect(paneMin, ImVec2(paneMin.x + ImGui::GetWindowSize().x,
                                          paneMin.y + ImGui::GetWindowSize().y));
    const AdaptiveSurface& surf =
        adaptivePanel(panelId("ai_ws_view_glass"), paneRect, GlassGroup::Global);

    // ---- aggregate every completed, model-stamped assistant turn across every session —
    // active (st.chat) + parked (Conversation::msgs) — pricing with cached Chudcode model
    // metadata. The same $/Mtok formula used by the per-session strip is computed client-side,
    // so Metrics needs no extra round-trip and remains available while catalog refresh is down.
    // Pricing provenance is carried through with the money, exactly like the cost strip: a model
    // the catalog has no price for adds tokens but NO dollars, so every total it touches is a
    // floor and is labelled one. `hasCost` cannot make that distinction (`cost` is an object
    // even when all four fields are zero, and live-only rows report cost:null with priced:false)
    // — `priced` can.
    double totalUsd = 0;
    long long totalIn = 0, totalOut = 0;
    int totalTurns = 0;
    int unpricedTurns = 0;
    std::vector<SessionSpend> sessions;
    std::unordered_map<std::string, ModelSpend> byModel;

    for (int i = 0; i < (int)st.conversations.size(); ++i) {
        const Conversation& c = st.conversations[i];
        const std::vector<ChatMsg>& msgs = (i == st.activeConv) ? st.chat : c.msgs;
        SessionSpend ss;
        ss.title = c.title.empty() ? ("Chat " + std::to_string(i + 1)) : c.title;
        ss.active = (i == st.activeConv);
        for (const auto& m : msgs) {
            if (m.fromUser || m.running || m.model.empty()) continue;
            long long tin = 0, tout = 0;
            for (const auto& a : m.activity) {
                if (a.rowKind != 0) continue;   // tool/ctx/provider rows aren't turn tokens
                tin += a.tokensIn;
                tout += a.tokensOut;
            }
            if (tin <= 0 && tout <= 0) continue;
            const BridgeModelMeta* meta = bridgeModelMeta("", m.model);
            const std::string prov = (meta && !meta->provider.empty()) ? meta->provider
                                                                       : std::string("unknown");
            const bool localFree = bridgeProviderIsLocalFree(prov);
            const bool priced = (meta && meta->priced);
            double usd = 0;
            if (priced)
                usd = (tin / 1.0e6) * meta->costIn + (tout / 1.0e6) * meta->costOut;

            ss.usd += usd; ss.tin += tin; ss.tout += tout; ss.turns++;
            totalUsd += usd; totalIn += tin; totalOut += tout; totalTurns++;
            if (!priced && !localFree) { ss.unpricedTurns++; unpricedTurns++; }

            ModelSpend& ms = byModel[prov + "/" + m.model];
            ms.provider = prov; ms.model = m.model;
            ms.priced = priced; ms.localFree = localFree;
            ms.usd += usd; ms.tin += tin; ms.tout += tout;
        }
        if (ss.turns > 0) sessions.push_back(std::move(ss));
    }

    if (totalTurns == 0) {
        float avail = ImGui::GetContentRegionAvail().y;
        ImGui::Dummy(ImVec2(0, avail * 0.35f));
        const char* msg = "No token usage yet";
        const char* sub = "Tokens and spend show up here once an assistant turn completes. "
                          "Models without catalog pricing still report tokens, but not dollars.";
        float mw = ImGui::CalcTextSize(msg).x;
        ImGui::SetCursorPosX(ImGui::GetCursorPosX() +
                             (ImGui::GetContentRegionAvail().x - mw) * 0.5f);
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(palette.textDim[0], palette.textDim[1], palette.textDim[2], 1.0f));
        ImGui::TextUnformatted(msg);
        ImGui::PopStyleColor();
        ImGui::Spacing();
        float avail2 = ImGui::GetContentRegionAvail().x;
        float wrapW  = std::min(avail2 - 16.0f * S, 460.0f * S);
        float sw     = ImGui::CalcTextSize(sub, nullptr, false, wrapW).x;
        ImGui::SetCursorPosX(ImGui::GetCursorPosX() + std::max(0.0f, (avail2 - sw) * 0.5f));
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(palette.textDisabled[0], palette.textDisabled[1],
                   palette.textDisabled[2], 0.75f));
        ImGui::PushTextWrapPos(ImGui::GetCursorPosX() + wrapW);
        ImGui::TextWrapped("%s", sub);
        ImGui::PopTextWrapPos();
        ImGui::PopStyleColor();
        return;
    }

    std::vector<ModelSpend> models;
    models.reserve(byModel.size());
    for (auto& kv : byModel) models.push_back(std::move(kv.second));
    // Priced rows first by money, then unpriced rows by traffic — an unpriced row's usd is 0 by
    // construction, so money alone would order them arbitrarily.
    std::sort(models.begin(), models.end(),
             [](const ModelSpend& a, const ModelSpend& b) {
        if (a.usd != b.usd) return a.usd > b.usd;
        return (a.tin + a.tout) > (b.tin + b.tout);
    });
    std::sort(sessions.begin(), sessions.end(),
             [](const SessionSpend& a, const SessionSpend& b) { return a.usd > b.usd; });

    // How much of the money is actually knowable: a model is "priced" here when the catalog
    // prices it, or when it is a local runtime where $0 is the real price.
    int knownPriceModels = 0;
    for (const auto& mrow : models)
        if (mrow.priced || mrow.localFree) ++knownPriceModels;
    const bool incomplete = (unpricedTurns > 0);

    // ---- headline totals ----
    secHeader("Overall");
    if (ImGui::BeginTable("##ai_metrics_totals", 5,
                          ImGuiTableFlags_SizingStretchSame | ImGuiTableFlags_NoPadOuterX)) {
        auto stat = [&](const char* label, const std::string& value, bool warn) {
            ImGui::TableNextColumn();
            ImGui::PushStyleColor(ImGuiCol_Text,
                ImVec4(palette.textDim[0], palette.textDim[1], palette.textDim[2], 1.0f));
            ImGui::TextUnformatted(label);
            ImGui::PopStyleColor();
            ImGui::PushFont(st.fonts.medium);
            if (warn) ImGui::PushStyleColor(ImGuiCol_Text, Palette::toU32(palette.statusWarn));
            ImGui::TextUnformatted(value.c_str());
            if (warn) ImGui::PopStyleColor();
            ImGui::PopFont();
        };
        // ">= " in ASCII: U+2265 is outside the font atlas (ui_theme.cpp uiGlyphRanges()).
        stat("Total spend", (incomplete ? ">= " : "") + fmtUsd(totalUsd), incomplete);
        stat("Total tokens", fmtTok(totalIn + totalOut), false);
        stat("Sessions tracked", std::to_string((int)sessions.size()), false);
        stat("Turns counted", std::to_string(totalTurns), false);
        stat("Models priced",
             std::to_string(knownPriceModels) + " of " + std::to_string((int)models.size()),
             incomplete);
        ImGui::EndTable();
    }
    ImGui::Dummy(ImVec2(0, ui::metrics::cpMenuSectionGap()));

    // ---- per-model breakdown ----
    secHeader("Spend by model");
    if (ImGui::BeginTable("##ai_metrics_models", 4,
                          ImGuiTableFlags_RowBg | ImGuiTableFlags_SizingStretchProp |
                          ImGuiTableFlags_NoPadOuterX)) {
        ImGui::TableSetupColumn("Model", ImGuiTableColumnFlags_WidthStretch, 2.4f);
        ImGui::TableSetupColumn("Provider", ImGuiTableColumnFlags_WidthStretch, 1.1f);
        ImGui::TableSetupColumn("Tokens (in / out)", ImGuiTableColumnFlags_WidthStretch, 1.6f);
        ImGui::TableSetupColumn("Spend", ImGuiTableColumnFlags_WidthStretch, 0.9f);
        ImGui::TableHeadersRow();
        for (const auto& mrow : models) {
            ImGui::TableNextRow();
            ImGui::TableNextColumn();
            ImGui::TextUnformatted(mrow.model.c_str());
            ImGui::TableNextColumn();
            ImGui::PushStyleColor(ImGuiCol_Text,
                ImVec4(palette.textDim[0], palette.textDim[1], palette.textDim[2], 1.0f));
            ImGui::TextUnformatted(mrow.provider.c_str());
            ImGui::PopStyleColor();
            ImGui::TableNextColumn();
            ImGui::Text("%s / %s", fmtTok(mrow.tin).c_str(), fmtTok(mrow.tout).c_str());
            ImGui::TableNextColumn();
            if (mrow.priced) {
                // Share of total spend as the shared circular gauge, not a mini-bar.
                fractionBadge(surf, totalUsd > 0.0 ? (float)(mrow.usd / totalUsd) : 0.0f,
                              fmtUsd(mrow.usd).c_str());
            } else {
                // No ring and no dollars: this row's 0 is an absence of price data, not a spend
                // of zero, and a gauge would render that absence as a measured share.
                ImGui::PushStyleColor(ImGuiCol_Text, mrow.localFree
                    ? ImVec4(palette.textDim[0], palette.textDim[1], palette.textDim[2], 1.0f)
                    : ImVec4(palette.statusWarn[0], palette.statusWarn[1],
                             palette.statusWarn[2], 1.0f));
                ImGui::TextUnformatted(mrow.localFree ? "free (local)" : "no price data");
                ImGui::PopStyleColor();
            }
        }
        ImGui::EndTable();
    }
    ImGui::Dummy(ImVec2(0, ui::metrics::cpMenuSectionGap()));

    // ---- per-session breakdown ----
    secHeader("Spend by session");
    if (ImGui::BeginTable("##ai_metrics_sessions", 4,
                          ImGuiTableFlags_RowBg | ImGuiTableFlags_SizingStretchProp |
                          ImGuiTableFlags_NoPadOuterX)) {
        ImGui::TableSetupColumn("Session", ImGuiTableColumnFlags_WidthStretch, 2.4f);
        ImGui::TableSetupColumn("Turns", ImGuiTableColumnFlags_WidthStretch, 0.7f);
        ImGui::TableSetupColumn("Tokens (in / out)", ImGuiTableColumnFlags_WidthStretch, 1.6f);
        ImGui::TableSetupColumn("Spend", ImGuiTableColumnFlags_WidthStretch, 0.9f);
        ImGui::TableHeadersRow();
        for (const auto& srow : sessions) {
            ImGui::TableNextRow();
            ImGui::TableNextColumn();
            if (srow.active) {
                ImGui::PushStyleColor(ImGuiCol_Text,
                    ImVec4(palette.accent[0], palette.accent[1], palette.accent[2], 1.0f));
                ImGui::TextUnformatted(srow.title.c_str());
                ImGui::PopStyleColor();
            } else {
                ImGui::TextUnformatted(srow.title.c_str());
            }
            ImGui::TableNextColumn();
            ImGui::Text("%d", srow.turns);
            ImGui::TableNextColumn();
            ImGui::Text("%s / %s", fmtTok(srow.tin).c_str(), fmtTok(srow.tout).c_str());
            ImGui::TableNextColumn();
            if (srow.unpricedTurns > 0) {
                // Mixed session: the dollars are real for its priced turns and missing for the
                // rest, so the cell is a floor and the ring's share would overstate certainty.
                ImGui::PushStyleColor(ImGuiCol_Text,
                    ImVec4(palette.statusWarn[0], palette.statusWarn[1],
                           palette.statusWarn[2], 1.0f));
                ImGui::Text(">= %s", fmtUsd(srow.usd).c_str());
                ImGui::PopStyleColor();
                if (ImGui::IsItemHovered())
                    ImGui::SetTooltip("%d of %d turns in this session ran on a model with no "
                                      "catalog price \xe2\x80\x94 their tokens are counted, "
                                      "their cost is unknown.", srow.unpricedTurns, srow.turns);
            } else {
                fractionBadge(surf, totalUsd > 0.0 ? (float)(srow.usd / totalUsd) : 0.0f,
                              fmtUsd(srow.usd).c_str());
            }
        }
        ImGui::EndTable();
    }
    ImGui::Dummy(ImVec2(0, ui::metrics::cpMenuSectionGap()));

    ImGui::PushStyleColor(ImGuiCol_Text,
        ImVec4(palette.textDisabled[0], palette.textDisabled[1], palette.textDisabled[2], 1.0f));
    if (incomplete)
        ImGui::TextWrapped(
            "Spend is catalog pricing \xc3\x97 tokens from completed turns \xe2\x80\x94 not a "
            "bill, and here not even a complete estimate: %d of %d models that spent tokens have "
            "no catalog price, so every \">=\" figure above is a floor. Those models' tokens "
            "are counted; their dollars are unknown, not zero.",
            (int)models.size() - knownPriceModels, (int)models.size());
    else
        ImGui::TextWrapped("Spend is catalog pricing \xc3\x97 tokens from completed turns "
                           "\xe2\x80\x94 not a bill, but every model that spent tokens here has "
                           "a catalog price. Turns with no token counts aren't listed.");
    ImGui::PopStyleColor();
}

}  // namespace ui
